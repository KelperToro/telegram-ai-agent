"""Provider adapters for Claude Code and OpenAI Codex CLI.

The bot has two independent axes:

* provider/engine: which agent CLI to run (Claude or Codex)
* exec_mode: how to run it (subprocess or tmux)

Keeping the provider-specific command and parser contracts here prevents
`exec_mode` from being overloaded with engine names.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from telegram_bot.core.services.cc_events import StreamEvent, _tool_status
from telegram_bot.core.services.codex_mcp import (
    build_codex_mcp_config_args,
    discover_codex_mcp_server_names,
)

logger = logging.getLogger(__name__)

Engine = Literal["claude", "codex"]
_CODEX_BOT_HOME = Path.home() / ".codex-bot"
_CODEX_HOME = Path.home() / ".codex"
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").lower() in _TRUTHY_ENV_VALUES


def _use_codex_bot_home() -> bool:
    return (
        not _env_truthy("TELEGRAM_CODEX_SHARED_HOME") and (_CODEX_BOT_HOME / "config.toml").exists()
    )


def _is_safe_owned_executable(path: Path) -> bool:
    """Return whether *path* is an executable owned only by the service user."""
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    if os.name == "nt":
        # Windows has ACLs rather than POSIX uid/mode ownership. Restrict
        # launchers to the executable formats used by Codex on Windows.
        return path.suffix.lower() in {".exe", ".cmd"}
    return stat.st_uid == getattr(os, "getuid")() and stat.st_mode & 0o022 == 0  # noqa: B009


def _configured_claude_binary() -> Path | None:
    configured = os.getenv("TELEGRAM_CLAUDE_BIN") or os.getenv("CLAUDE_BINARY_PATH")
    if not configured:
        return None
    return Path(configured).expanduser()


def _standalone_claude_path() -> Path:
    return Path.home() / ".local" / "bin" / "claude"


def _legacy_claude_path() -> Path:
    return Path.home() / ".npm-global" / "bin" / "claude"


def claude_binary() -> str:
    """Return a safe absolute Claude CLI path for service and cron processes."""
    configured = _configured_claude_binary()
    if configured is not None:
        if configured.is_absolute() and _is_safe_owned_executable(configured):
            return str(configured)
        raise RuntimeError(f"Unsafe configured Claude binary: {configured}")

    candidates = [_standalone_claude_path()]
    if found := shutil.which("claude"):
        candidates.append(Path(found))
    candidates.append(_legacy_claude_path())

    for candidate in candidates:
        if candidate.is_absolute() and _is_safe_owned_executable(candidate):
            return str(candidate)

    # Fail with a deterministic path rather than relying on the service PATH.
    return str(_standalone_claude_path())


def safe_claude_binary() -> str | None:
    """Resolve Claude or return ``None`` when no safe executable is available."""
    try:
        candidate = Path(claude_binary())
    except RuntimeError:
        return None
    if candidate.is_absolute() and _is_safe_owned_executable(candidate):
        return str(candidate)
    return None


def _ensure_codex_global_skill_links() -> None:
    """Expose global Codex skills inside the bot's isolated CODEX_HOME."""
    source_root = _CODEX_HOME / "skills"
    target_root = _CODEX_BOT_HOME / "skills"
    if not source_root.is_dir() or not target_root.is_dir():
        return

    for target in target_root.iterdir():
        if not target.is_symlink():
            continue
        try:
            resolved = target.resolve(strict=True)
        except FileNotFoundError:
            target.unlink()
            continue
        try:
            resolved.relative_to(source_root)
        except ValueError:
            continue
        if not (resolved / "SKILL.md").is_file():
            target.unlink()

    for source in source_root.iterdir():
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            continue
        target = target_root / source.name
        if target.exists() or target.is_symlink():
            continue
        target.symlink_to(source, target_is_directory=True)


def engine_display_name(engine: str) -> str:
    """Return a human-facing provider name for chat notifications."""
    if engine == "codex":
        return "Codex"
    if engine == "claude":
        return "Claude Code"
    return engine


def _codex_tui_prefix() -> list[str]:
    """Select the Codex home configured for bot processes.

    The production launch config sets ``TELEGRAM_CODEX_SHARED_HOME=1`` so bot
    sessions use the regular ~/.codex home. The isolated ~/.codex-bot behavior
    remains available when that flag is absent.
    """
    return codex_env_prefix()


def _configured_codex_binary() -> Path | None:
    configured = os.getenv("TELEGRAM_CODEX_BIN") or os.getenv("CODEX_BINARY_PATH")
    if not configured:
        return None
    return Path(configured).expanduser()


def _standalone_codex_path() -> Path:
    return Path.home() / ".local" / "bin" / "codex"


def codex_npm_prefix() -> Path:
    """Return the legacy npm global prefix used only as a last-resort fallback."""
    configured = os.getenv("CODEX_NPM_PREFIX") or os.getenv("TELEGRAM_CODEX_NPM_PREFIX")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / "npm"
    return Path.home() / ".npm-global"


# Non-secret application paths needed by the reusable runtime. Applications
# can register additional non-secret names without putting their behavior in core.
_AGENT_APP_ENV = {
    "APP_ROOT",
    "AGENT_WORKSPACE_ROOT",
    "PROJECT_ROOT",
}

_CODEX_ENV_ALLOWLIST = {
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "COLORTERM",
    "NO_COLOR",
    "TERM",
    "TMUX_TMPDIR",
    "CODEX_HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "HOMEDRIVE",
    "HOMEPATH",
    *_AGENT_APP_ENV,
}


def extend_agent_env_allowlist(names: set[str]) -> None:
    """Allow application-owned, non-secret variables in launched agent processes."""
    _AGENT_APP_ENV.update(names)
    _CODEX_ENV_ALLOWLIST.update(names)


def agent_process_env(
    *,
    binary: str | Path | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the non-secret environment shared by bot-launched agents."""
    source = os.environ if base_env is None else base_env
    env = {key: value for key, value in source.items() if key in _CODEX_ENV_ALLOWLIST}
    if binary is not None:
        bin_dir = Path(binary).expanduser().parent
        current_path = env.get("PATH", os.defpath)
        env["PATH"] = os.pathsep.join(
            dict.fromkeys(
                [str(bin_dir), *(part for part in current_path.split(os.pathsep) if part)]
            )
        )
    env.pop("NPM_CONFIG_PREFIX", None)
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("TERM", "xterm-256color")
    return env


def agent_env_prefix(*, binary: str | Path | None = None) -> list[str]:
    """Return an ``env -i`` prefix for tmux-launched agent processes."""
    env = agent_process_env(binary=binary)
    return ["env", "-i", *(f"{key}={value}" for key, value in sorted(env.items()))]


def codex_process_env(
    *,
    codex_bin: str | Path | None = None,
    base_env: dict[str, str] | None = None,
    minimal: bool = True,
) -> dict[str, str]:
    """Build a constrained process env for bot-spawned Codex commands."""
    binary = Path(codex_bin).expanduser() if codex_bin is not None else Path(CODEX_ADAPTER.binary())
    if minimal:
        env = agent_process_env(binary=binary, base_env=base_env)
    else:
        env = dict(os.environ if base_env is None else base_env)
        current_path = env.get("PATH", os.defpath)
        env["PATH"] = os.pathsep.join(
            dict.fromkeys([str(binary.expanduser().parent), *current_path.split(os.pathsep)])
        )
        env.pop("NPM_CONFIG_PREFIX", None)
        env.setdefault("HOME", str(Path.home()))
        env.setdefault("TERM", "xterm-256color")
    if _use_codex_bot_home():
        _ensure_codex_global_skill_links()
        env["CODEX_HOME"] = str(_CODEX_BOT_HOME)
    elif os.name != "nt":
        env.pop("CODEX_HOME", None)
    return env


def codex_env_prefix(*, codex_bin: str | Path | None = None) -> list[str]:
    env = codex_process_env(codex_bin=codex_bin)
    if os.name == "nt":
        env["TERM"] = "xterm-256color"
        return ["env", "-i", *(f"{key}={value}" for key, value in sorted(env.items()))]
    keys = ["CODEX_HOME", "PATH"]
    inherited_keys = [
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "COLORTERM",
        "NO_COLOR",
        "TERM",
        *_AGENT_APP_ENV,
    ]
    ordered_keys = [*inherited_keys, *keys]
    return ["env", "-i", *(f"{key}={env[key]}" for key in ordered_keys if key in env)]


def _codex_sessions_root(home: Path | None = None) -> Path:
    if home is not None:
        return home / ".codex" / "sessions"
    if _use_codex_bot_home():
        return _CODEX_BOT_HOME / "sessions"
    if configured := os.getenv("CODEX_HOME"):
        return Path(configured) / "sessions"
    return Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class ExecCommand:
    argv: list[str]
    cwd: str
    stdin_text: str | None = None
    output_last_message_path: Path | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class ExecParseResult:
    events: list[StreamEvent]
    session_id: str | None = None


@dataclass(frozen=True)
class TuiParseResult:
    events: list[StreamEvent]
    session_id: str | None = None
    done: bool = False


@dataclass(frozen=True)
class TuiSessionInfo:
    session_id: str
    transcript_path: Path
    tail_start_offset: int = 0


@dataclass(frozen=True)
class TranscriptFileState:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class CodexTranscriptSnapshot:
    root: Path
    files: dict[Path, TranscriptFileState]


class ProviderAdapter(Protocol):
    name: Engine

    def parse_exec_event(self, raw: str) -> ExecParseResult: ...

    def build_tui_start(
        self, *, cwd: str, model: str | None = None, mcp_config: str | None = None
    ) -> list[str]: ...

    def build_tui_resume(
        self,
        *,
        cwd: str,
        session_id: str,
        model: str | None = None,
        mcp_config: str | None = None,
    ) -> list[str]: ...

    def parse_tui_event(self, raw: str) -> TuiParseResult: ...

    def is_prompt_ready(self, pane: str) -> bool: ...

    def is_modal_present(self, pane: str) -> bool: ...

    def transcript_path_for_state(
        self, *, cwd: str, session_id: str, transcript_path: str | None
    ) -> Path | None: ...


def _load_json(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class CodexAdapter:
    name: Engine = "codex"
    _MODAL_TAIL_LINES = 20

    @staticmethod
    def _is_subagent_source(source: object) -> bool:
        if isinstance(source, dict):
            return "subagent" in source
        if isinstance(source, str):
            return source == "subagent"
        return False

    def _meta_session_id(self, payload: dict[str, Any], *, cwd: str) -> str | None:
        if payload.get("originator") != "codex-tui" or not self._cwd_matches(
            payload.get("cwd"), cwd
        ):
            return None
        if self._is_subagent_source(payload.get("source")):
            return None
        session_id = payload.get("id")
        return session_id if isinstance(session_id, str) and session_id else None

    @staticmethod
    def _cwd_matches(value: object, cwd: str) -> bool:
        if not isinstance(value, str):
            return False
        if value == cwd:
            return True
        try:
            return Path(value).resolve() == Path(cwd).resolve()
        except OSError:
            return False

    def capture_tui_transcript_snapshot(
        self, *, home: Path | None = None
    ) -> CodexTranscriptSnapshot:
        root = _codex_sessions_root(home)
        files: dict[Path, TranscriptFileState] = {}
        for path in root.glob("**/*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files[path.resolve()] = TranscriptFileState(
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        return CodexTranscriptSnapshot(root=root, files=files)

    @staticmethod
    def _iter_complete_json_lines_from(path: Path, offset: int) -> list[dict[str, Any]]:
        try:
            with path.open("rb") as f:
                f.seek(offset)
                raw = f.read()
        except OSError:
            return []
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            parts = raw.rsplit(b"\n", 1)
            raw = b"" if len(parts) == 1 else parts[0] + b"\n"
        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            data = _load_json(line.decode("utf-8", errors="replace"))
            if data is not None:
                records.append(data)
        return records

    def _session_id_from_meta(self, path: Path, *, cwd: str, max_records: int = 5) -> str | None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f):
                    if idx >= max_records:
                        break
                    data = _load_json(line)
                    if not data or data.get("type") != "session_meta":
                        continue
                    payload = data.get("payload")
                    if isinstance(payload, dict):
                        return self._meta_session_id(payload, cwd=cwd)
        except OSError:
            return None
        return None

    @staticmethod
    def _normalize_prompt_for_match(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        return value.rstrip("\r\n")

    @classmethod
    def _prompts_match(cls, value: object, prompt: str) -> bool:
        return cls._normalize_prompt_for_match(value) == cls._normalize_prompt_for_match(prompt)

    @classmethod
    def _tui_user_message_text(cls, data: dict[str, Any]) -> str | None:
        if data.get("type") != "event_msg":
            return None
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return None
        if payload.get("type") == "user_message":
            return cls._message_text_from_payload(payload)
        if payload.get("type") != "item_completed":
            return None
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "UserMessage":
            return None
        return cls._message_text_from_payload(item)

    @classmethod
    def _has_prompt_user_message(cls, records: list[dict[str, Any]], prompt: str) -> int:
        matches = 0
        for data in records:
            if cls._prompts_match(cls._tui_user_message_text(data), prompt):
                matches += 1
        return matches

    def has_prompt_user_message_after(self, path: Path, *, offset: int, prompt: str) -> bool:
        """True when a Codex TUI transcript records this prompt after offset."""
        records = self._iter_complete_json_lines_from(path, offset)
        return self._has_prompt_user_message(records, prompt) > 0

    @staticmethod
    def _command_from_exec_payload(payload: dict[str, Any]) -> str | None:
        command = payload.get("command")
        if isinstance(command, list) and all(isinstance(part, str) for part in command):
            parts = [str(part) for part in command]
            if len(parts) >= 3 and parts[0].endswith("bash") and parts[1] == "-lc":
                return parts[2]
            return " ".join(parts)
        if isinstance(command, str) and command:
            return command

        parsed_cmd = payload.get("parsed_cmd")
        if isinstance(parsed_cmd, list):
            for item in parsed_cmd:
                if isinstance(item, dict):
                    cmd = item.get("cmd")
                    if isinstance(cmd, str) and cmd:
                        return cmd
        return None

    @staticmethod
    def _message_text_from_payload(payload: dict[str, Any]) -> str | None:
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
        text = payload.get("text")
        if isinstance(text, str) and text:
            return text
        content = payload.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_text = item.get("text")
                if isinstance(item_text, str) and item_text:
                    parts.append(item_text)
            if parts:
                return "".join(parts)
        return None

    @staticmethod
    def _status_for_codex_function_call(name: str, tool_input: dict[str, object] | None) -> str:
        if name.startswith("functions."):
            name = name.split(".", 1)[1]
        if name == "exec_command" and isinstance(tool_input, dict):
            cmd = tool_input.get("cmd")
            if isinstance(cmd, str) and cmd:
                return _tool_status("Bash", {"command": cmd})
        return _tool_status(name, tool_input)

    def binary(self) -> str:
        """Return an executable Codex CLI path that works in service processes.

        The bot service may not inherit the interactive shell PATH. Prefer the
        standalone installer path (or an explicit operator override), and keep
        the old npm-global location only as a legacy fallback.
        """
        configured = _configured_codex_binary()
        if configured is not None:
            if configured.is_absolute() and self._is_safe_binary(configured):
                return str(configured)
            raise RuntimeError(f"Unsafe configured Codex binary: {configured}")

        if os.name == "nt":
            candidates = []
            if found := shutil.which("codex.exe"):
                candidates.append(Path(found))
            local_app_data = os.getenv("LOCALAPPDATA")
            if local_app_data:
                candidates.extend(
                    (Path(local_app_data) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe")
                )
            if found := shutil.which("codex.cmd"):
                candidates.append(Path(found))
            candidates.append(codex_npm_prefix() / "codex.cmd")
        else:
            candidates = [
                _standalone_codex_path(),
                _CODEX_HOME / "packages" / "standalone" / "current" / "bin" / "codex",
            ]
            if found := shutil.which("codex"):
                candidates.append(Path(found))
            candidates.append(codex_npm_prefix() / "bin" / "codex")

        for candidate in candidates:
            if candidate.is_absolute() and self._is_safe_binary(candidate):
                return str(candidate)

        # Return the expected standalone path so process spawn fails loudly
        # instead of searching a service PATH that may not contain Codex.
        return str(candidates[0] if candidates else _standalone_codex_path())

    def safe_binary(self) -> str | None:
        candidate = Path(self.binary())
        if candidate.is_absolute() and self._is_safe_binary(candidate):
            return str(candidate)
        return None

    @staticmethod
    def _is_safe_binary(path: Path) -> bool:
        return _is_safe_owned_executable(path)

    def parse_exec_event(self, raw: str) -> ExecParseResult:
        data = _load_json(raw)
        if data is None:
            return ExecParseResult([])

        event_type = data.get("type")
        if event_type == "thread.started":
            thread_id = data.get("thread_id")
            return ExecParseResult([], thread_id if isinstance(thread_id, str) else None)

        payload = data.get("payload")
        if event_type == "response_item" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "function_call":
                name = payload.get("name", "")
                args = payload.get("arguments")
                tool_input: dict[str, object] | None = None
                if isinstance(args, str):
                    parsed_args = _load_json(args)
                    tool_input = parsed_args if parsed_args is not None else None
                elif isinstance(args, dict):
                    tool_input = args
                status = self._status_for_codex_function_call(str(name), tool_input)
                return ExecParseResult([StreamEvent("status", status)])
            if payload_type == "tool_search_call":
                return ExecParseResult([StreamEvent("status", "Ищу инструмент...")])
            if payload_type == "message" and payload.get("role") == "assistant":
                if payload.get("phase") == "final_answer":
                    return ExecParseResult([])
                text = self._message_text_from_payload(payload)
                if text:
                    return ExecParseResult([StreamEvent("text", text)])
                return ExecParseResult([])

        if event_type == "event_msg" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "agent_message":
                if payload.get("phase") == "final_answer":
                    return ExecParseResult([])
                text = self._message_text_from_payload(payload)
                if text:
                    return ExecParseResult([StreamEvent("text", text)])
                return ExecParseResult([])
            if payload_type == "exec_command_end":
                exit_code = payload.get("exit_code")
                if not isinstance(exit_code, int) or exit_code == 0:
                    return ExecParseResult([])
                command = self._command_from_exec_payload(payload)
                status = _tool_status(
                    "Bash",
                    {"command": command} if isinstance(command, str) else None,
                )
                return ExecParseResult([StreamEvent("status", f"{status} (exit {exit_code})")])

        item = data.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "command_execution":
                command = item.get("command")
                exit_code = item.get("exit_code")
                if event_type == "item.completed" and (
                    not isinstance(exit_code, int) or exit_code == 0
                ):
                    return ExecParseResult([])
                status = _tool_status(
                    "Bash",
                    {"command": command} if isinstance(command, str) else None,
                )
                if event_type == "item.completed" and isinstance(exit_code, int) and exit_code:
                    status = f"{status} (exit {exit_code})"
                return ExecParseResult([StreamEvent("status", status)])
            if event_type == "item.completed" and item_type == "agent_message":
                # Final answer is read from --output-last-message after exit.
                return ExecParseResult([])

        return ExecParseResult([])

    def build_tui_start(
        self, *, cwd: str, model: str | None = None, mcp_config: str | None = None
    ) -> list[str]:
        codex_home = _CODEX_BOT_HOME if _use_codex_bot_home() else _CODEX_HOME
        inherited_servers = discover_codex_mcp_server_names(cwd, codex_home=codex_home)
        cmd = [
            *_codex_tui_prefix(),
            self.binary(),
            *build_codex_mcp_config_args(
                mcp_config,
                inherited_server_names=inherited_servers,
            ),
            "--no-alt-screen",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            cwd,
        ]
        if model:
            cmd.extend(["--model", model])
        return cmd

    def build_tui_resume(
        self,
        *,
        cwd: str,
        session_id: str,
        model: str | None = None,
        mcp_config: str | None = None,
    ) -> list[str]:
        codex_home = _CODEX_BOT_HOME if _use_codex_bot_home() else _CODEX_HOME
        inherited_servers = discover_codex_mcp_server_names(cwd, codex_home=codex_home)
        cmd = [
            *_codex_tui_prefix(),
            self.binary(),
            "resume",
            *build_codex_mcp_config_args(
                mcp_config,
                inherited_server_names=inherited_servers,
            ),
            session_id,
            "--no-alt-screen",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            cwd,
        ]
        if model:
            cmd.extend(["--model", model])
        return cmd

    def parse_tui_event(self, raw: str) -> TuiParseResult:
        data = _load_json(raw)
        if data is None:
            return TuiParseResult([])

        event_type = data.get("type")
        payload = data.get("payload")
        if event_type == "session_meta" and isinstance(payload, dict):
            session_id = payload.get("id")
            return TuiParseResult(
                [],
                session_id=session_id if isinstance(session_id, str) else None,
            )

        if event_type == "event_msg" and isinstance(payload, dict):
            ptype = payload.get("type")
            if ptype == "agent_message":
                message = payload.get("message")
                if not isinstance(message, str) or not message:
                    return TuiParseResult([])
                if payload.get("phase") == "final_answer":
                    return TuiParseResult([StreamEvent("result_message", message)])
                return TuiParseResult([StreamEvent("text", message)])
            if ptype == "exec_command_end":
                exit_code = payload.get("exit_code")
                if not isinstance(exit_code, int) or exit_code == 0:
                    return TuiParseResult([])

                command = self._command_from_exec_payload(payload)
                status = _tool_status(
                    "Bash",
                    {"command": command} if isinstance(command, str) else None,
                )
                return TuiParseResult([StreamEvent("status", f"{status} (exit {exit_code})")])
            if ptype == "item_completed":
                item = payload.get("item")
                if not isinstance(item, dict):
                    return TuiParseResult([])
                item_type = item.get("type")
                if item_type == "AgentMessage":
                    message = self._message_text_from_payload(item)
                    if not message:
                        return TuiParseResult([])
                    if item.get("phase") == "final_answer":
                        return TuiParseResult([StreamEvent("result_message", message)])
                    return TuiParseResult([StreamEvent("text", message)])
                if item_type == "CommandExecution":
                    exit_code = item.get("exit_code")
                    if not isinstance(exit_code, int) or exit_code == 0:
                        return TuiParseResult([])
                    command = self._command_from_exec_payload(item)
                    status = _tool_status(
                        "Bash",
                        {"command": command} if isinstance(command, str) else None,
                    )
                    return TuiParseResult([StreamEvent("status", f"{status} (exit {exit_code})")])
            if ptype == "task_complete":
                return TuiParseResult([StreamEvent("result", "")], done=True)

        if (
            event_type == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") in {"function_call", "custom_tool_call"}
        ):
            name = payload.get("name", "")
            args = (
                payload.get("arguments")
                if payload.get("type") == "function_call"
                else payload.get("input")
            )
            tool_input: dict[str, object] | None = None
            if isinstance(args, str):
                parsed_args = _load_json(args)
                tool_input = parsed_args if parsed_args is not None else None
            elif isinstance(args, dict):
                tool_input = args
            status = self._status_for_codex_function_call(str(name), tool_input)
            return TuiParseResult([StreamEvent("status", status)])

        # Assistant response_item messages are intentionally ignored:
        # Codex also emits event_msg agent_message for commentary/final answers,
        # and that path is the single delivery source to avoid duplicates.

        return TuiParseResult([])

    def is_prompt_ready(self, pane: str) -> bool:
        return "\u203a" in pane

    def is_modal_present(self, pane: str) -> bool:
        # Codex leaves prior dialogs in scrollback after they are dismissed.
        # Trim physical blank padding and inspect only the live tail, otherwise
        # old "trust this directory" or normal output mentioning settings.json
        # produces repeated false modal alerts while the agent is simply working.
        lines = pane.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        tail = "\n".join(lines[-self._MODAL_TAIL_LINES :]).lower()
        markers = (
            "allow command",
            "approval required",
            "do you trust the contents of this directory",
            "select model and effort",
            "press enter to confirm",
            "press enter to continue",
            "press enter to select",
            "enter to confirm",
            "esc to cancel",
            "esc to dismiss",
            "no, quit",
            # Codex Question dialog (multi-choice with optional notes).
            # Footer: `tab to add notes | enter to submit answer | esc to interrupt`.
            # Without these markers the bot saw codex's interactive picker as
            # plain idle pane and never surfaced the TUI keyboard, so the user
            # could not answer the question from Telegram (reported 2026-04-26).
            "enter to submit answer",
            "tab to add notes",
        )
        return any(marker in tail for marker in markers)

    def transcript_path_for_state(
        self, *, cwd: str, session_id: str, transcript_path: str | None
    ) -> Path | None:
        if transcript_path:
            path = Path(transcript_path)
            return path if path.exists() else None
        return self.find_tui_transcript(cwd=cwd, session_id=session_id)

    def find_tui_transcript(
        self, *, cwd: str, session_id: str, home: Path | None = None
    ) -> Path | None:
        """Find one existing Codex TUI transcript for ``session_id`` and ``cwd``.

        Collisions fail closed: returning None is safer than tailing an
        arbitrary transcript from a different run.
        """
        root = _codex_sessions_root(home)
        matches: list[Path] = []
        for path in root.glob(f"**/*{session_id}*.jsonl"):
            try:
                first = path.read_text(errors="replace").splitlines()[0]
                data = _load_json(first)
            except (OSError, IndexError):
                continue
            if not data or data.get("type") != "session_meta":
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                continue
            if self._meta_session_id(payload, cwd=cwd) == session_id:
                matches.append(path.resolve())
        if len(matches) == 1:
            return matches[0]
        return None

    async def locate_tui_transcript(
        self,
        *,
        cwd: str,
        snapshot: CodexTranscriptSnapshot,
        prompt: str,
        home: Path | None = None,
        timeout_sec: float = 30.0,
    ) -> TuiSessionInfo:
        root = _codex_sessions_root(home) if home is not None else snapshot.root
        deadline = time.monotonic() + timeout_sec
        settle_sec = min(0.25, timeout_sec)
        single_since: float | None = None
        single_identity: tuple[str, Path, int] | None = None
        last_matches: list[tuple[str, Path, int]] = []
        while True:
            candidates: list[TuiSessionInfo] = []
            candidate_meta: list[tuple[str, Path, int]] = []
            for path in root.glob("**/*.jsonl"):
                resolved = path.resolve()
                try:
                    stat = path.stat()
                except OSError:
                    continue
                previous = snapshot.files.get(resolved)
                if previous is not None and stat.st_size <= previous.size:
                    continue
                baseline = (
                    previous.size if previous is not None and stat.st_size >= previous.size else 0
                )
                session_id = self._session_id_from_meta(path, cwd=cwd)
                if not session_id:
                    continue
                records = self._iter_complete_json_lines_from(path, baseline)
                prompt_matches = self._has_prompt_user_message(records, prompt)
                if prompt_matches == 0:
                    continue
                for _ in range(prompt_matches):
                    candidates.append(TuiSessionInfo(session_id, resolved, baseline))
                    candidate_meta.append(
                        (
                            session_id,
                            resolved,
                            baseline,
                        )
                    )
            last_matches = candidate_meta
            if len(candidates) > 1:
                logger.warning(
                    "Codex TUI transcript prompt collision for cwd=%s candidates=%s",
                    cwd,
                    candidate_meta,
                )
                raise RuntimeError("Codex TUI transcript collision")
            if len(candidates) == 1:
                identity = (
                    candidates[0].session_id,
                    candidates[0].transcript_path,
                    candidates[0].tail_start_offset,
                )
                now = time.monotonic()
                if single_identity != identity:
                    single_identity = identity
                    single_since = now
                if single_since is not None and (
                    now - single_since >= settle_sec or now >= deadline
                ):
                    return candidates[0]
            else:
                single_identity = None
                single_since = None
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(0.2, max(deadline - time.monotonic(), 0.0)))
        logger.warning(
            "Codex TUI transcript not found for cwd=%s root=%s snapshot_files=%d matches=%s",
            cwd,
            root,
            len(snapshot.files),
            last_matches,
        )
        raise TimeoutError("Codex TUI transcript not found")


class CodexTranscriptParser:
    """Per-tail Codex parser that attaches events to ``turn_context.turn_id``."""

    def __init__(self, adapter: CodexAdapter | None = None) -> None:
        self._adapter = adapter or CODEX_ADAPTER
        self._turn_id: str | None = None
        self._final_seen = False
        self._completed_turn_id: str | None = None
        self._completed_final_seen = False

    @property
    def current_turn_id(self) -> str | None:
        return self._turn_id

    @staticmethod
    def is_turn_boundary(raw: str) -> bool:
        return CodexTranscriptParser.turn_boundary_id(raw) is not None

    @staticmethod
    def turn_boundary_id(raw: str) -> str | None:
        data = _load_json(raw)
        if data is None or data.get("type") != "turn_context":
            return None
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return None
        turn_id = payload.get("turn_id")
        return turn_id if isinstance(turn_id, str) and turn_id else None

    def parse(self, raw: str) -> TuiParseResult:
        data = _load_json(raw)
        if data is None:
            return TuiParseResult([])
        payload = data.get("payload")

        if data.get("type") == "turn_context" and isinstance(payload, dict):
            raw_turn_id = payload.get("turn_id")
            if not isinstance(raw_turn_id, str) or not raw_turn_id:
                return TuiParseResult([])
            if raw_turn_id == self._completed_turn_id:
                return TuiParseResult([])
            if raw_turn_id == self._turn_id:
                return TuiParseResult([])
            events: list[StreamEvent] = []
            if self._turn_id is not None:
                events.append(StreamEvent("turn_end", "", turn_id=self._turn_id))
            self._turn_id = raw_turn_id
            self._final_seen = False
            self._completed_turn_id = None
            self._completed_final_seen = False
            events.append(StreamEvent("turn_start", "", turn_id=raw_turn_id))
            return TuiParseResult(events)

        if self._adapter._tui_user_message_text(data) is not None and self._turn_id is not None:
            # Codex keeps a clarification in the current native turn even
            # after emitting a final_answer. Reopen exactly one final window
            # without inventing another lifecycle turn.
            if self._final_seen:
                self._final_seen = False
            return TuiParseResult([])

        if (
            data.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "task_complete"
        ):
            raw_turn_id = payload.get("turn_id")
            completed_id = raw_turn_id if isinstance(raw_turn_id, str) else self._turn_id
            if completed_id is None:
                return TuiParseResult([])
            if self._turn_id is not None and completed_id != self._turn_id:
                logger.warning(
                    "Ignoring stale Codex task_complete turn_id=%s active_turn_id=%s",
                    completed_id,
                    self._turn_id,
                )
                return TuiParseResult([])
            self._completed_turn_id = completed_id
            self._completed_final_seen = self._final_seen
            self._turn_id = None
            return TuiParseResult(
                [StreamEvent("turn_end", "", turn_id=completed_id)],
                done=False,
            )

        parsed = self._adapter.parse_tui_event(raw)
        normalized_events: list[StreamEvent] = []
        event_turn_id = self._turn_id or self._completed_turn_id
        for event in parsed.events:
            if event.type == "result":
                continue
            if event.type == "result_message":
                final_seen = (
                    self._final_seen if self._turn_id is not None else self._completed_final_seen
                )
                if final_seen:
                    logger.warning(
                        "Ignoring duplicate Codex final_answer turn_id=%s",
                        event_turn_id,
                    )
                    continue
                if self._turn_id is not None:
                    self._final_seen = True
                else:
                    self._completed_final_seen = True
            event.turn_id = event_turn_id
            normalized_events.append(event)
        return TuiParseResult(
            normalized_events,
            session_id=parsed.session_id,
            done=False,
        )


CODEX_ADAPTER = CodexAdapter()


def is_engine_available(engine: str) -> bool:
    """Return whether the provider CLI can be spawned by the current process."""
    if engine == "claude":
        return safe_claude_binary() is not None
    if engine == "codex":
        try:
            return CODEX_ADAPTER._is_safe_binary(Path(CODEX_ADAPTER.binary()))
        except RuntimeError:
            return False
    return False


def choose_available_engine(preferred: str = "claude") -> Engine | None:
    """Pick an installed engine, preferring the requested one and then the other."""
    if preferred in {"claude", "codex"} and is_engine_available(preferred):
        return preferred  # type: ignore[return-value]
    fallback: Engine = "codex" if preferred == "claude" else "claude"
    if is_engine_available(fallback):
        return fallback
    return None
