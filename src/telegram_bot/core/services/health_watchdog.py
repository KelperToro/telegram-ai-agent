"""Resource health watchdog — alerts the owner on cgroup OOM kills and
tmux-server death.

Background (2026-06-17 incident): an 8-subagent swarm drove the
``telegram-bot.service`` cgroup (MemoryHigh 5G / MemoryMax 6G) to a 5.37G
peak; the kernel cgroup OOM killer fired (``memory.events:oom_kill``) and
took down the *shared* tmux server, silently killing every topic's Claude
Code session at once. The bot process itself survived, so nothing surfaced
to the user — the running task just stopped, and a later message spawned a
fresh session with no memory of the work.

This watchdog polls two cheap signals on a cadence and posts a Telegram
alert so such an event is never silent again:

1. cgroup ``memory.events`` ``oom_kill`` counter — increments whenever the
   kernel OOM-kills any process in the bot's cgroup (the root cause).
2. tmux server pid — a change from one live server to a different one means
   the shared server died and every CC session on it was lost (the visible
   symptom). A bare disappearance (pid → None) is *not* alerted: that is the
   normal ``exit-empty`` shutdown when the last idle session ends.

Reads are injected as callables so the loop is trivially testable; the
real implementations are the module-level ``read_*`` functions wired up in
``__main__``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from telegram_bot.core.services.windows_pty import run_tmux

logger = logging.getLogger(__name__)

_GB = 1024**3
_DEFAULT_INTERVAL_SEC = 30.0


@dataclass(frozen=True)
class MemoryStats:
    """Cgroup memory snapshot in bytes. ``None`` for an unreadable field or
    an unbounded ``memory.max`` (the literal ``"max"``)."""

    current: int | None
    peak: int | None
    max: int | None


# --- Readers (module-level so __main__ can wire them; pure I/O) ---


def read_own_cgroup_dir() -> Path | None:
    """Resolve the sysfs dir of this process's cgroup v2 group, or None.

    Parses the single ``0::<path>`` line of ``/proc/self/cgroup`` and joins
    it under ``/sys/fs/cgroup``. Returns None on cgroup v1, missing sysfs,
    or any read error — callers then disable OOM polling gracefully.
    """
    try:
        content = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            path = Path("/sys/fs/cgroup") / parts[2].strip().lstrip("/")
            return path if path.is_dir() else None
    return None


def read_oom_kill_count(cgroup_dir: Path) -> int | None:
    """Return the ``oom_kill`` counter from ``memory.events``, or None.

    The counter is cumulative since the cgroup was created (bot start), so
    the watchdog compares against a baseline rather than the absolute value.
    """
    try:
        content = (cgroup_dir / "memory.events").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        if line.startswith("oom_kill "):
            with contextlib.suppress(IndexError, ValueError):
                return int(line.split()[1])
            return None
    return None


def read_memory_stats(cgroup_dir: Path) -> MemoryStats:
    """Read ``memory.current`` / ``memory.peak`` / ``memory.max`` in bytes."""

    def _read_int(name: str) -> int | None:
        try:
            raw = (cgroup_dir / name).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if raw == "max":  # unbounded limit
            return None
        with contextlib.suppress(ValueError):
            return int(raw)
        return None

    return MemoryStats(
        current=_read_int("memory.current"),
        peak=_read_int("memory.peak"),
        max=_read_int("memory.max"),
    )


def read_tmux_server_pid() -> int | None:
    """Return the default-socket tmux server pid, or None if no server runs.

    ``tmux display-message -p '#{pid}'`` resolves the server pid from any
    live session. Exit-non-zero ("no server running") or a non-numeric
    payload both map to None.
    """
    try:
        result = run_tmux(
            ["tmux", "display-message", "-p", "#{pid}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    with contextlib.suppress(ValueError):
        return int(result.stdout.strip())
    return None


# --- Formatting ---


def _fmt_bytes(value: int | None) -> str:
    return f"{value / _GB:.2f} ГБ" if value is not None else "—"


# --- Watchdog ---


class HealthWatchdog:
    """Polls OOM and tmux-server signals on a cadence and alerts on change.

    The first tick only captures baselines (it never alerts) so a restart
    doesn't re-announce OOM kills that happened before this run. Per-tick
    read errors are swallowed — a transient unreadable counter must not kill
    the loop.
    """

    def __init__(
        self,
        *,
        on_alert: Callable[[str], Awaitable[None]],
        read_oom_kill: Callable[[], int | None],
        read_memory_stats: Callable[[], MemoryStats],
        read_tmux_pid: Callable[[], int | None],
        active_session_count: Callable[[], int],
        interval_sec: float = _DEFAULT_INTERVAL_SEC,
    ) -> None:
        self._on_alert = on_alert
        self._read_oom_kill = read_oom_kill
        self._read_memory_stats = read_memory_stats
        self._read_tmux_pid = read_tmux_pid
        self._active_session_count = active_session_count
        self._interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None
        # None until the first tick observes a value; thereafter the
        # last-seen baseline used to detect change.
        self._oom_baseline: int | None = None
        self._last_server_pid: int | None = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Launch the background loop. Idempotent."""
        if self.is_running():
            return
        self._task = asyncio.create_task(self._loop(), name="health-watchdog")
        logger.info("HEALTH_WATCHDOG started interval=%.1fs", self._interval_sec)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_sec)
                try:
                    await self.check_once()
                except Exception:
                    logger.warning("HEALTH_WATCHDOG tick failed", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def check_once(self) -> None:
        """One poll cycle. Public so tests can drive it deterministically."""
        await self._check_oom()
        await self._check_tmux_server()

    async def _check_oom(self) -> None:
        count = self._read_oom_kill()
        if count is None:
            return
        if self._oom_baseline is None:
            self._oom_baseline = count
            return
        if count <= self._oom_baseline:
            return
        delta = count - self._oom_baseline
        stats = self._read_memory_stats()
        sessions = self._active_session_count()
        logger.warning(
            "HEALTH_WATCHDOG oom_kill detected delta=%d total=%d sessions=%d "
            "mem_peak=%s mem_max=%s",
            delta,
            count,
            sessions,
            stats.peak,
            stats.max,
        )
        # Advance the baseline only after the alert is delivered: if the send
        # raises (transient network/timeout), the next tick re-detects the same
        # delta and re-announces rather than swallowing the OOM kill silently.
        await self._on_alert(
            "⚠️ <b>OOM-kill в cgroup бота</b>\n"
            f"Ядро убило процессов: <b>+{delta}</b> (всего за аптайм: {count})\n"
            f"Память: пик {_fmt_bytes(stats.peak)} · "
            f"сейчас {_fmt_bytes(stats.current)} · лимит {_fmt_bytes(stats.max)}\n"
            f"Активных CC-сессий: {sessions}. "
            "Задачи в топиках могли молча оборваться."
        )
        self._oom_baseline = count

    async def _check_tmux_server(self) -> None:
        pid = self._read_tmux_pid()
        if pid is None:
            # No server right now (idle exit-empty or a death not yet
            # followed by a respawn). Keep the last live pid so the next
            # spawn is recognised as a replacement.
            return
        previous = self._last_server_pid
        if previous is None or pid == previous:
            self._last_server_pid = pid
            return
        logger.warning("HEALTH_WATCHDOG tmux server replaced old_pid=%d new_pid=%d", previous, pid)
        # Advance only after a successful alert (see _check_oom): a failed send
        # leaves the old pid so the replacement is re-announced next tick.
        await self._on_alert(
            "⚠️ <b>tmux-сервер был пересоздан</b>\n"
            f"PID {previous} → {pid}: прежний сервер умер, "
            "все CC-сессии на нём потеряны.\n"
            "Открытую задачу в топике нужно запустить заново."
        )
        self._last_server_pid = pid
