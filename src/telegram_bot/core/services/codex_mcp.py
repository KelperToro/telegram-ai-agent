"""Safe per-topic MCP bridge for OpenAI Codex CLI.

Codex CLI does not accept Claude-style ``--mcp-config`` files. The bot passes
Codex TOML overrides that point each configured MCP server at this runner. The
runner reads the original JSON config inside the child process and launches
the real server with its env, keeping secret env values out of Codex argv.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CREDENTIAL_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@:]+:[^/@]+@", re.IGNORECASE)
_CREDENTIAL_OPTION_RE = re.compile(
    r"(?:api[-_]?key|access[-_]?token|auth[-_]?token|bearer[-_]?token|"
    r"client[-_]?secret|credentials?|password|passwd|secret|session[-_]?string|token)",
    re.IGNORECASE,
)


def _toml_value(value: object) -> str:
    """Return a TOML-compatible literal for simple Codex ``-c`` values."""
    return json.dumps(value, ensure_ascii=True)


def _toml_key(value: str) -> str:
    """Return a safe TOML dotted-key component."""
    return value if _SERVER_NAME_RE.fullmatch(value) else _toml_value(value)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"MCP config symlink path is not allowed: {current}")


def _load_config(mcp_config: str) -> tuple[dict[str, Any], str]:
    path = Path(os.path.abspath(Path(mcp_config).expanduser()))
    try:
        _reject_symlink_components(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"MCP config must be a regular file: {path}")
            if os.name != "nt":
                if metadata.st_uid != getattr(os, "getuid")():  # noqa: B009
                    raise ValueError(f"MCP config must be owned by the service user: {path}")
                if metadata.st_mode & 0o022:
                    raise ValueError(f"MCP config is group/world writable: {path}")
            raw = handle.read()
        data = json.loads(raw.decode("utf-8"))
    except OSError as exc:
        raise ValueError(f"MCP config is not readable: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"MCP config is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"MCP config is invalid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("MCP config root must be an object")
    return data, hashlib.sha256(raw).hexdigest()


def _has_secret_bearing_arg(args: list[str]) -> bool:
    for value in args:
        option = value.split("=", 1)[0]
        is_credential_option = option.startswith("-") and _CREDENTIAL_OPTION_RE.search(option)
        is_auth_header = "authorization:" in value.lower() or "authorization=" in value.lower()
        if is_credential_option or is_auth_header or _CREDENTIAL_URL_RE.match(value):
            return True
    return False


def _server_items(mcp_config: str) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    data, digest = _load_config(mcp_config)
    servers = data.get("mcpServers")
    if servers is None:
        return [], digest
    if not isinstance(servers, dict):
        raise ValueError("MCP config mcpServers must be an object")

    result: list[tuple[str, dict[str, Any]]] = []
    for name, server in servers.items():
        if not isinstance(name, str) or not _SERVER_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid MCP server name: {name!r}")
        if not isinstance(server, dict):
            raise ValueError(f"MCP server {name!r} must be an object")
        args = server.get("args") or []
        if (
            isinstance(args, list)
            and all(isinstance(item, str) for item in args)
            and _has_secret_bearing_arg(args)
        ):
            raise ValueError(
                f"MCP server {name!r} has secret-bearing argv; use protected env instead"
            )
        result.append((name, server))
    return result, digest


def build_codex_mcp_config_args(
    mcp_config: str | None,
    *,
    ignore_user_config: bool = False,
    inherited_server_names: set[str] | None = None,
) -> list[str]:
    """Build Codex CLI args for topic-scoped MCP servers.

    Env values from the MCP JSON are intentionally omitted from argv. They are
    loaded by this module when Codex starts the server process.
    """
    args = ["--ignore-user-config"] if ignore_user_config else []
    for name in sorted(inherited_server_names or set()):
        args.extend(["-c", f"mcp_servers.{_toml_key(name)}.enabled=false"])
    if not mcp_config:
        return args

    unresolved_path = Path(os.path.abspath(Path(mcp_config).expanduser()))
    if not unresolved_path.exists():
        return args
    servers, digest = _server_items(str(unresolved_path))
    path = str(unresolved_path)
    for name, _server in servers:
        runner_args = ["-m", __name__, path, name, digest]
        args.extend(
            [
                "-c",
                f"mcp_servers.{name}.command={_toml_value(sys.executable)}",
                "-c",
                f"mcp_servers.{name}.args={_toml_value(runner_args)}",
                "-c",
                f"mcp_servers.{name}.enabled=true",
            ]
        )
    return args


def discover_codex_mcp_server_names(
    cwd: str | Path,
    *,
    codex_home: str | Path | None = None,
) -> set[str]:
    """Collect MCP names inherited from Codex user and project config layers."""
    resolved_cwd = Path(cwd).resolve()
    home = (
        Path(codex_home).expanduser().resolve()
        if codex_home is not None
        else (Path.home() / ".codex").resolve()
    )
    candidates = [home / "config.toml"]
    project_candidates = [
        parent / ".codex" / "config.toml"
        for parent in reversed((resolved_cwd, *resolved_cwd.parents))
    ]
    candidates.extend(path for path in project_candidates if path not in candidates)

    names: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Codex config is not readable: {path}") from exc
        servers = data.get("mcp_servers")
        if not isinstance(servers, dict):
            continue
        names.update(name for name in servers if isinstance(name, str))
    return names


def load_mcp_server(
    mcp_config: str,
    server_name: str,
    *,
    expected_digest: str | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Load one MCP server entry and return ``command, argv, env`` for launch."""
    if not _SERVER_NAME_RE.fullmatch(server_name):
        raise ValueError(f"Invalid MCP server name: {server_name!r}")

    server_items, digest = _server_items(mcp_config)
    if expected_digest is not None and digest != expected_digest:
        raise ValueError("MCP config changed after validation")
    servers = dict(server_items)
    server = servers.get(server_name)
    if server is None:
        raise ValueError(f"Unknown MCP server: {server_name}")

    command = server.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError(f"MCP server {server_name!r} command must be a non-empty string")

    raw_args = server.get("args", [])
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list) or not all(isinstance(item, str) for item in raw_args):
        raise ValueError(f"MCP server {server_name!r} args must be a list of strings")

    raw_env = server.get("env", {})
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()
    ):
        raise ValueError(f"MCP server {server_name!r} env must be an object of strings")

    env = dict(os.environ)
    env.update(raw_env)
    return command, [command, *raw_args], env


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m telegram_bot.core.services.codex_mcp``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in {2, 3}:
        print("usage: codex_mcp <mcp_config_path> <server_name> [sha256]", file=sys.stderr)
        return 2
    try:
        command, exec_argv, env = load_mcp_server(
            argv[0],
            argv[1],
            expected_digest=argv[2] if len(argv) == 3 else None,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if os.name == "nt":
        # Windows cannot reliably replace this Python process with execvpe.
        # Inherit stdio so Codex still speaks MCP directly to the child server.
        try:
            return subprocess.run(exec_argv, env=env, check=False).returncode
        except OSError as exc:
            print(f"Could not start MCP server {argv[1]!r}: {exc}", file=sys.stderr)
            return 1
    os.execvpe(command, exec_argv, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
