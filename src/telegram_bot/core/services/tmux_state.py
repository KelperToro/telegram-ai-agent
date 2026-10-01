"""Persistent state for TmuxManager — dataclass, JSON store, orphan scan.

Extracted from `tmux_manager.py` to keep the manager facade small.
Public symbols are re-exported from `tmux_manager` so callers do not need
to know the internal split.

`StateStore.save()` is guarded by `threading.Lock` so concurrent writers
cannot race on the `tmp → os.replace()` sequence. Without the lock, two
simultaneous `_save_state()` calls could both write the same tmp path
and one of the renames could fail or leave a partial file behind
(observed intermittently under high tail activity).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from telegram_bot.core.services.claude import Mode
from telegram_bot.core.services.windows_pty import run_tmux
from telegram_bot.core.types import ChannelKey
from telegram_bot.core.utils.file_lock import FileLock

logger = logging.getLogger(__name__)


@dataclass
class TmuxSessionState:
    session_name: str
    session_dir: str
    session_id: str | None
    mode: Mode
    cwd: str
    mcp_config: str
    chat_id: int
    offset: int = 0  # bytes already sent to Telegram
    # Legacy state.json entries get "stream-json-legacy" via _normalize_state_dict.
    runner_version: str = "tui-v1"
    provider: str = "claude"
    model: str | None = None
    transcript_path: str | None = None
    base_mcp_config: str | None = None


@dataclass(frozen=True)
class StateLoadResult:
    ok: bool
    raw: dict[str, Any]
    error: Exception | None = None


@dataclass(frozen=True)
class StateEntryParseResult:
    state: TmuxSessionState | None
    error: Exception | None = None


_STATE_FIELD_NAMES = {field.name for field in fields(TmuxSessionState)}
_SIGNIFICANT_BACKUP_FIELDS = {
    "session_id",
    "provider",
    "cwd",
    "runner_version",
}


def _normalize_state_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Fill in runner_version for legacy state.json entries.

    Old stream-json runner wrote entries without runner_version. We can't rely
    on the dataclass default because that would silently label legacy sessions
    as "tui-v1" and mislead downstream migration logic. Returns a new dict so
    the caller's input is not mutated.
    """
    normalized = dict(data)
    if "runner_version" not in normalized:
        normalized["runner_version"] = "stream-json-legacy"
    if "provider" not in normalized:
        normalized["provider"] = "claude"
    if normalized.get("runner_version") == "tui-v1":
        normalized["runner_version"] = "claude-tui-v1"
    normalized.setdefault("model", None)
    normalized.setdefault("transcript_path", None)
    normalized.setdefault("base_mcp_config", normalized.get("mcp_config"))
    return normalized


def parse_state_entry(raw: Any) -> StateEntryParseResult:
    """Parse one durable state entry without rejecting unknown future fields."""
    if not isinstance(raw, dict):
        return StateEntryParseResult(None, TypeError("state entry is not an object"))
    normalized = _normalize_state_dict(raw)
    filtered = {key: value for key, value in normalized.items() if key in _STATE_FIELD_NAMES}
    try:
        return StateEntryParseResult(TmuxSessionState(**filtered))
    except Exception as exc:
        return StateEntryParseResult(None, exc)


class StateStore:
    """JSON-backed store for the sessions map. Thread-safe around writes.

    The lock is `threading.Lock` (not asyncio) because the tail loop calls
    `save` from multiple task contexts (send_stream, _run_recovery_tail,
    clear_context) and we don't control which event-loop iteration commits
    the write. A threading lock serialises the critical section across
    tasks without requiring callers to hold an asyncio lock.
    """

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        self._write_lock = threading.Lock()
        self._poisoned = False

    @property
    def path(self) -> Path:
        return self._state_path

    def exists(self) -> bool:
        return self._state_path.exists()

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    def load(self) -> StateLoadResult:
        """Load durable state, marking invalid/non-object JSON as poisoned."""
        if not self._state_path.exists():
            self._poisoned = False
            return StateLoadResult(ok=True, raw={})
        try:
            raw = json.loads(self._state_path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("tmux state top-level JSON is not an object")
        except Exception as exc:
            logger.warning("Failed to load tmux state", exc_info=True)
            self._poisoned = True
            self._backup_unreadable_state()
            return StateLoadResult(ok=False, raw={}, error=exc)
        self._poisoned = False
        return StateLoadResult(ok=True, raw=raw)

    def load_raw(self) -> dict[str, Any]:
        """Return parsed state.json as dict, or {} when the store is poisoned."""
        return self.load().raw

    def save(self, sessions: dict[Any, TmuxSessionState]) -> bool:
        """Atomic write: tmp + os.replace() so a crash between write and rename
        leaves the previous valid state.json intact. A direct write_text
        truncates the target file first — a crash then yields a partial
        JSON that breaks restore_all on the next bot startup.

        `sessions` is typed loosely because ChannelKey is a tuple and
        cannot be a dict key in JSON — we serialise "chat_id:thread_id".
        """
        with self._write_lock:
            try:
                with self._file_lock():
                    raw, poisoned = self._read_current_raw_under_lock()
                    if poisoned:
                        logger.warning(
                            "Refusing to save tmux state because durable state is poisoned"
                        )
                        return False
                    data = self._merge_live_sessions(raw, sessions)
                    self._backup_if_significant_change(raw, data)
                    self._atomic_write_json(data)
                    return True
            except Exception:
                logger.warning("Failed to save tmux state", exc_info=True)
                return False

    def remove(self, channel_key: Any, *, reason: str) -> bool:
        """Explicitly remove a durable entry. Runtime failures must not call this."""
        key_str = f"{channel_key[0]}:{channel_key[1]}"
        with self._write_lock:
            try:
                with self._file_lock():
                    raw, poisoned = self._read_current_raw_under_lock()
                    if poisoned:
                        logger.warning(
                            "Refusing to remove tmux state for %s because durable state is "
                            "poisoned",
                            key_str,
                        )
                        return False
                    if key_str not in raw:
                        return True
                    data = dict(raw)
                    data.pop(key_str, None)
                    self._write_history(key_str, reason)
                    self._backup_raw(raw)
                    self._atomic_write_json(data)
                    return True
            except Exception:
                logger.warning("Failed to remove tmux state for %s", key_str, exc_info=True)
                return False

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self._state_path):
            yield

    def _atomic_write_json(self, data: dict[str, Any]) -> None:
        tmp_path = self._state_path.with_name(
            f"{self._state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            # Close fd manually only when fdopen itself fails: once fdopen
            # succeeds the file object owns the fd, and an extra os.close after
            # `with` would double-close — possibly killing an fd another thread
            # already reused.
            try:
                f = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise
            with f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._state_path)
            self._fsync_dir(self._state_path.parent)
        except Exception:
            logger.warning("Failed to save tmux state", exc_info=True)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def _read_current_raw_under_lock(self) -> tuple[dict[str, Any], bool]:
        if self._poisoned:
            return {}, True
        if not self._state_path.exists():
            return {}, False
        try:
            raw = json.loads(self._state_path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("tmux state top-level JSON is not an object")
        except Exception:
            logger.warning("Failed to load tmux state during save", exc_info=True)
            self._poisoned = True
            self._backup_unreadable_state()
            return {}, True
        return raw, False

    def _merge_live_sessions(
        self,
        raw: dict[str, Any],
        sessions: dict[Any, TmuxSessionState],
    ) -> dict[str, Any]:
        data = dict(raw)
        for channel_key, state in sessions.items():
            key_str = f"{channel_key[0]}:{channel_key[1]}"
            previous = data.get(key_str)
            merged = dict(previous) if isinstance(previous, dict) else {}
            merged.update(asdict(state))
            data[key_str] = merged
        return data

    def _backup_if_significant_change(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        if not before:
            return
        if set(before) - set(after):
            self._backup_raw(before)
            return
        for key, old_entry in before.items():
            new_entry = after.get(key)
            if not isinstance(old_entry, dict) or not isinstance(new_entry, dict):
                if old_entry != new_entry:
                    self._backup_raw(before)
                    return
                continue
            if any(
                old_entry.get(field) != new_entry.get(field) for field in _SIGNIFICANT_BACKUP_FIELDS
            ):
                self._backup_raw(before)
                return

    def _backup_unreadable_state(self) -> None:
        with contextlib.suppress(OSError):
            raw_text = self._state_path.read_text()
            self._write_backup_text(raw_text)

    def _backup_raw(self, raw: dict[str, Any]) -> None:
        self._write_backup_text(json.dumps(raw, indent=2))

    def _write_backup_text(self, text: str) -> None:
        backup_dir = self._state_path.parent / "state.backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"state-{time.time_ns()}.json"
        fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        self._prune_backups(backup_dir)
        self._fsync_dir(backup_dir)

    @staticmethod
    def _prune_backups(backup_dir: Path) -> None:
        backups = sorted(
            (path for path in backup_dir.glob("state-*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        total = 0
        for idx, path in enumerate(backups):
            size = path.stat().st_size
            total += size
            if idx >= 10 or total > 5 * 1024 * 1024:
                with contextlib.suppress(OSError):
                    path.unlink()

    def _write_history(self, key_str: str, reason: str) -> None:
        history_path = self._state_path.parent / "state.history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": time.time(), "key": key_str, "reason": reason}
        fd = os.open(history_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        with contextlib.suppress(OSError):
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def peek_saved_session(store: StateStore, channel_key: ChannelKey, cwd: str) -> str | None:
    """Return a safely-resumable session_id from state.json, or None.

    All guards must pass:

      1. An entry exists in state.json for this channel_key.
      2. `runner_version == "tui-v1"` — legacy stream-json entries cannot
         be resumed via `--resume` (different transcript format).
      3. `session_id` is non-empty.
      4. `state.cwd == cwd` — topic-cwd change invalidates the resume
         because the saved session points at a different codebase.
      5. The transcript jsonl is present on disk — otherwise CC would
         exit with "Error: Session <uuid> not found" under `--resume`.

    `transcript_path` is resolved lazily through `tmux_manager` so tests
    that patch `telegram_bot.core.services.tmux_manager.transcript_path`
    take effect here too.
    """
    if not store.exists():
        return None
    result = store.load()
    if not result.ok:
        logger.warning("peek_saved_session: state.json unreadable; refusing lazy resume")
        return None
    raw = result.raw

    key_str = f"{channel_key[0]}:{channel_key[1]}"
    entry = raw.get(key_str) if isinstance(raw, dict) else None
    if not isinstance(entry, dict):
        return None

    data = _normalize_state_dict(entry)
    rv = data.get("runner_version")
    provider = data.get("provider", "claude")
    if not (
        (provider == "claude" and rv in {"tui-v1", "claude-tui-v1"})
        or (provider == "codex" and rv == "codex-tui-v1")
    ):
        logger.debug(
            "peek_saved_session: %s runner_version=%s unsupported, skipping",
            key_str,
            data.get("runner_version"),
        )
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if data.get("cwd") != cwd:
        logger.debug(
            "peek_saved_session: %s cwd changed (state=%s, current=%s), skipping",
            key_str,
            data.get("cwd"),
            cwd,
        )
        return None
    if provider == "codex":
        raw_path = data.get("transcript_path")
        path = Path(raw_path) if isinstance(raw_path, str) else None
    else:
        # Lazy import via the facade so monkey-patches of
        # `tmux_manager.transcript_path` (test harness idiom) take effect.
        from telegram_bot.core.services import tmux_manager as _tm

        path = _tm.transcript_path(cwd, session_id)  # type: ignore[attr-defined]
    if path is None or not path.exists():
        return None
    return session_id


def scan_orphan_tmux_sessions(state_path: Path) -> list[str]:
    """Return sorted names of cc-* tmux sessions that are not tui-v1 in state.

    Used by bot startup (Wave 4 Task 5) to surface tmux sessions left over
    from the legacy stream-json runner. "Orphan" = tmux session named cc-*
    that either has no entry in state.json or whose entry lacks
    runner_version="tui-v1".

    Graceful failure modes (all return []):
    - state_path missing or unreadable JSON
    - tmux binary unavailable (FileNotFoundError)
    - tmux ls returns non-zero (no server / no sessions)

    Does not touch tmux or files beyond reading — safe to call before the
    TmuxManager is instantiated.
    """
    state_markers: dict[str, str] = {}
    state_result = StateStore(state_path).load()
    if state_result.ok:
        for entry in state_result.raw.values():
            if not isinstance(entry, dict):
                continue
            name = entry.get("session_name")
            if not isinstance(name, str):
                continue
            state_markers[name] = entry.get("runner_version", "stream-json-legacy")

    try:
        tmux_result = run_tmux(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.debug("scan_orphan_tmux_sessions: tmux binary not found")
        return []
    except Exception:
        logger.debug("scan_orphan_tmux_sessions: tmux ls failed", exc_info=True)
        return []

    if tmux_result.returncode != 0:
        logger.debug("scan_orphan_tmux_sessions: tmux ls returncode=%d", tmux_result.returncode)
        return []

    orphans: list[str] = []
    for raw_name in tmux_result.stdout.splitlines():
        name = raw_name.strip()
        if not name or not name.startswith("cc-"):
            continue
        marker = state_markers.get(name)
        if marker not in {"tui-v1", "claude-tui-v1", "codex-tui-v1"}:
            orphans.append(name)

    return sorted(orphans)
