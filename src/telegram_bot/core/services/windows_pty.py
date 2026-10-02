"""Persistent, local ConPTY sessions used in place of tmux on Windows.

The broker is a separate process so a bot restart does not close its console
handles.  Only loopback clients with the random per-broker auth key may talk
to it.  The public ``run_tmux`` adapter mirrors the small tmux command set the
bot uses; Linux continues to invoke tmux itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any, cast

from telegram_bot.core.utils.file_lock import FileLock

_BROKER_FILE_ENV = "TELEGRAM_BOT_PTY_BROKER_FILE"
_KEYS = {
    "Enter": "\r",
    "Escape": "\x1b",
    "BSpace": "\x7f",
    "Tab": "\t",
    "BTab": "\x1b[Z",
    "Space": " ",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "C-c": "\x03",
    "C-u": "\x15",
    "C-o": "\x0f",
    "C-r": "\x12",
    "C-t": "\x14",
    "C-b": "\x02",
}


def configure_broker(sessions_dir: Path) -> None:
    """Choose a stable broker address for all processes in this bot install."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    local_root = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "telegram-ai-agent" / "pty"
    )
    local_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(sessions_dir.resolve()).casefold().encode()).hexdigest()[:16]
    os.environ[_BROKER_FILE_ENV] = str(local_root / f"broker-{digest}.json")


def _broker_file() -> Path:
    configured = os.environ.get(_BROKER_FILE_ENV)
    if not configured:
        raise RuntimeError("Windows PTY broker was not configured")
    return Path(configured)


def _connect(info: dict[str, Any]) -> Any:
    return Client(("127.0.0.1", int(info["port"])), authkey=bytes.fromhex(info["key"]))


def _request(
    message: dict[str, Any], *, create: bool = False, timeout: float | None = None
) -> dict[str, Any]:
    path = _broker_file()
    if create:
        _ensure_broker(path)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        with _connect(info) as connection:
            connection.send(message)
            if not connection.poll(timeout if timeout is not None else 15.0):
                raise TimeoutError("Windows PTY broker response timed out")
            result = connection.recv()
    except (OSError, EOFError, ValueError, KeyError, TimeoutError) as exc:
        raise RuntimeError("Windows PTY broker is unavailable") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Invalid Windows PTY broker response")
    return result


def _ensure_broker(path: Path) -> None:
    from telegram_bot.core.services.providers import agent_process_env

    with FileLock(path):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            with _connect(info) as connection:
                connection.send({"op": "ping"})
                if connection.poll(2.0) and connection.recv().get("ok"):
                    return
        except (OSError, EOFError, ValueError, KeyError, AttributeError):
            pass
        key = secrets.token_bytes(32)
        args = [sys.executable, "-m", __name__, "--broker", str(path), key.hex()]
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            args,
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=agent_process_env(),
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                if info.get("key") == key.hex():
                    with _connect(info) as connection:
                        connection.send({"op": "ping"})
                        if connection.poll(2.0) and connection.recv().get("ok"):
                            return
            except (OSError, EOFError, ValueError, KeyError, AttributeError):
                pass
            time.sleep(0.1)
    raise RuntimeError("Windows PTY broker did not start")


def _session_name(argv: Sequence[str]) -> str:
    try:
        target = argv[argv.index("-t") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("tmux target is missing") from exc
    return target.removeprefix("=").removesuffix(":")


def _parse_spawn(argv: Sequence[str], env: Mapping[str, str] | None) -> dict[str, Any]:
    name = argv[argv.index("-s") + 1]
    x_index = argv.index("-x")
    y_index = argv.index("-y")
    cols = int(argv[x_index + 1])
    rows = int(argv[y_index + 1])
    command = list(argv[y_index + 2 :])
    launch_env = dict(env or os.environ)
    if command[:2] == ["env", "-i"]:
        launch_env = {}
        command = command[2:]
        while command and "=" in command[0]:
            key, value = command.pop(0).split("=", 1)
            launch_env[key] = value
    if not command:
        raise ValueError("PTY launch command is empty")
    # A service/IDE can export TERM=dumb; ConPTY supports VT sequences.
    launch_env["TERM"] = "xterm-256color"
    if Path(command[0]).suffix.lower() in {".cmd", ".bat"}:
        command = [launch_env.get("COMSPEC", "cmd.exe"), "/d", "/c", *command]
    return {
        "op": "spawn",
        "name": name,
        "argv": command,
        "env": launch_env,
        "cols": cols,
        "rows": rows,
    }


def run_tmux(
    argv: Sequence[str],
    *,
    capture_output: bool = False,
    text: bool = False,
    check: bool = False,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run tmux on Unix or emulate its used commands through ConPTY on Windows."""
    if os.name != "nt" or not argv or argv[0] != "tmux":
        return subprocess.run(
            argv,
            capture_output=capture_output,
            text=text,
            check=check,
            cwd=cwd,
            env=env,
            input=input,
            timeout=timeout,
            **kwargs,
        )
    command = argv[1] if len(argv) > 1 else ""
    try:
        if command == "new-session":
            message = _parse_spawn(argv, env)
            message["cwd"] = str(cwd or Path.cwd())
        elif command == "display-message" and "-t" not in argv:
            message = {"op": "server-pid"}
        elif command in {
            "has-session",
            "kill-session",
            "capture-pane",
            "display-message",
            "send-keys",
        }:
            message = {"op": command, "name": _session_name(argv), "argv": list(argv)}
        elif command in {"ls", "list-sessions"}:
            message = {"op": "list-sessions"}
        elif command == "show-environment":
            message = {"op": "show-environment"}
        elif command == "set-environment":
            message = {"op": "set-environment"}
        elif command == "load-buffer":
            message = {
                "op": "load-buffer",
                "name": argv[argv.index("-b") + 1],
                "data": input.decode("utf-8") if isinstance(input, bytes) else input or "",
            }
        elif command == "paste-buffer":
            message = {
                "op": "paste-buffer",
                "name": _session_name(argv),
                "buffer": argv[argv.index("-b") + 1],
            }
        elif command == "delete-buffer":
            message = {"op": "delete-buffer", "buffer": argv[argv.index("-b") + 1]}
        else:
            raise ValueError(f"Unsupported tmux command: {command}")
        result = _request(message, create=command == "new-session", timeout=timeout)
        returncode = 0 if result.get("ok") else 1
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("error", ""))
    except (RuntimeError, ValueError, IndexError) as exc:
        returncode, stdout, stderr = 1, "", str(exc)
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, argv, output=stdout, stderr=stderr)
    if not text:
        return subprocess.CompletedProcess(argv, returncode, stdout.encode(), stderr.encode())
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class _Session:
    def __init__(self, process: Any, cols: int, rows: int) -> None:
        import pyte

        self.process = process
        self.cols = cols
        self.screen = pyte.HistoryScreen(cols, rows, history=2000)
        self.stream = pyte.Stream(self.screen)
        self.lock = threading.RLock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            while True:
                chunk = self.process.read(65536)
                if chunk:
                    with self.lock:
                        self.stream.feed(chunk)
        except (EOFError, OSError):
            pass

    def capture(self, history_lines: int) -> str:
        with self.lock:
            lines: list[str] = []
            if history_lines:
                for row in list(self.screen.history.top)[-history_lines:]:
                    width = max(row, default=-1) + 1
                    lines.append("".join(row[col].data for col in range(width)).rstrip())
            lines.extend(line.rstrip() for line in self.screen.display)
            return "\n".join(lines).rstrip("\n") + "\n"


class _Broker:
    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}
        self.buffers: dict[str, str] = {}
        self.lock = threading.RLock()

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "ping":
            return {"ok": True}
        if op in {"show-environment", "set-environment"}:
            return {"ok": True, "stdout": ""}
        if op == "server-pid":
            return {"ok": True, "stdout": f"{os.getpid()}\n"}
        if op == "spawn":
            from winpty import Backend, PtyProcess  # type: ignore[import-untyped]

            name = request["name"]
            with self.lock:
                old = self.sessions.pop(name, None)
                if old:
                    old.process.close(force=True)
                process = PtyProcess.spawn(
                    request["argv"],
                    cwd=request["cwd"],
                    env=request["env"],
                    dimensions=(request["rows"], request["cols"]),
                    # pywinpty treats integer 0 as an unset argument and can
                    # fall back to PYWINPTY_BACKEND. Its truthy string form
                    # is converted to 0 internally and forces native ConPTY.
                    backend=str(Backend.ConPTY),
                )
                self.sessions[name] = _Session(process, request["cols"], request["rows"])
            return {"ok": True}
        if op == "list-sessions":
            with self.lock:
                names = sorted(
                    name for name, session in self.sessions.items() if session.process.isalive()
                )
            return {"ok": True, "stdout": "\n".join(names) + ("\n" if names else "")}
        if op == "load-buffer":
            self.buffers[request["name"]] = request["data"]
            return {"ok": True}
        if op == "delete-buffer":
            self.buffers.pop(request["buffer"], None)
            return {"ok": True}
        name = request.get("name", "")
        with self.lock:
            session = self.sessions.get(name)
        if session is None or (not session.process.isalive() and op != "capture-pane"):
            return {"ok": False, "error": f"no session: {name}"}
        if op == "has-session":
            return {"ok": True}
        if op == "kill-session":
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["taskkill", "/PID", str(session.process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            session.process.close(force=True)
            with self.lock:
                self.sessions.pop(name, None)
            return {"ok": True}
        if op == "capture-pane":
            argv = request["argv"]
            try:
                history_lines = abs(int(argv[argv.index("-S") + 1]))
            except (ValueError, IndexError):
                history_lines = 0
            return {"ok": True, "stdout": session.capture(history_lines)}
        if op == "display-message":
            fmt = request["argv"][-1]
            value = {
                "#{pane_pid}": session.process.pid,
                "#{pane_width}": session.cols,
                "#{pid}": os.getpid(),
            }.get(fmt, "")
            return {"ok": True, "stdout": f"{value}\n"}
        if op == "paste-buffer":
            payload = self.buffers.get(request["buffer"])
            if payload is None:
                return {"ok": False, "error": "buffer missing"}
            # ConPTY turns VT input into Windows console events. Codex's
            # Windows reader receives literal marker text from bracketed
            # paste, so send the payload itself through the PTY.
            session.process.write(payload)
            return {"ok": True}
        if op == "send-keys":
            argv = request["argv"]
            key_start = argv.index("-t") + 2
            literal = "-l" in argv[2:]
            for key in argv[key_start:]:
                if key == "-l":
                    continue
                sequence = key if literal else _KEYS.get(key, key)
                session.process.write(sequence)
            return {"ok": True}
        return {"ok": False, "error": f"unsupported operation: {op}"}


def _serve(path: Path, key: bytes) -> None:
    listener = Listener(("127.0.0.1", 0), authkey=key)
    host, port = cast(tuple[str, int], listener.address)
    assert host == "127.0.0.1"
    descriptor = {"pid": os.getpid(), "port": port, "key": key.hex()}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(descriptor), encoding="utf-8")
    os.replace(temp, path)
    broker = _Broker()
    while True:
        try:
            connection = listener.accept()
        except AuthenticationError:
            continue
        try:
            request = connection.recv()
            try:
                result = broker.handle(request)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            connection.send(result)
        except (EOFError, OSError):
            pass
        finally:
            connection.close()


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--broker":
    _serve(Path(sys.argv[2]), bytes.fromhex(sys.argv[3]))
