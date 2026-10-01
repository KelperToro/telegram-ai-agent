from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from telegram_bot.core.config import Settings
from telegram_bot.core.env_file import read_exact_env_file
from telegram_bot.core.handlers import commands
from telegram_bot.core.handlers.tail import handle_tail_command
from telegram_bot.core.services import cc_modes
from telegram_bot.core.services.bot_commands import build_bot_commands
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.providers import (
    CODEX_ADAPTER,
    CodexTranscriptParser,
    agent_process_env,
    choose_available_engine,
)
from telegram_bot.core.services.rich_sender import detect_rich_send
from telegram_bot.core.services.tmux_spawn import (
    sanitized_tmux_environment,
    tmux_pane_inherits_disallowed_environment,
)
from telegram_bot.core.services.topic_config import (
    TopicConfig,
    TopicSettings,
)
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config
from telegram_bot.core.tui.transcript import ClaudeTranscriptParser


def test_public_entrypoint_imports() -> None:
    entrypoint = importlib.import_module("telegram_bot.__main__")

    assert callable(entrypoint.main)
    assert callable(entrypoint.make_recovery_on_event)


def test_public_entrypoint_selects_dedicated_tmux_server(tmp_path: Path, monkeypatch) -> None:
    entrypoint = importlib.import_module("telegram_bot.__main__")
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/custom/default,123,0")
    monkeypatch.setattr(
        entrypoint.subprocess,
        "run",
        MagicMock(return_value=MagicMock(returncode=1, stdout="")),
    )

    runtime_dir = entrypoint._ensure_dedicated_tmux_tmpdir(tmp_path, tmp_path / "tmux_sessions")

    assert runtime_dir == tmp_path / ".telegram-bot-tmux"
    if os.name != "nt":
        assert runtime_dir.stat().st_mode & 0o777 == 0o700
    assert entrypoint.os.environ["TMUX_TMPDIR"] == str(runtime_dir)


def test_public_entrypoint_migrates_only_state_owned_legacy_tmux_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    entrypoint = importlib.import_module("telegram_bot.__main__")
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/custom/default,123,0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    sessions_dir = tmp_path / "tmux_sessions"
    sessions_dir.mkdir()
    (sessions_dir / "state.json").write_text(
        json.dumps({"1:None": {"session_name": "cc-1-0"}}),
        encoding="utf-8",
    )

    def _run(command, **_kwargs):
        if command[:2] == ["tmux", "list-sessions"]:
            return MagicMock(returncode=0, stdout="cc-1-0\nunrelated\n")
        return MagicMock(returncode=0, stdout="")

    run = MagicMock(side_effect=_run)
    monkeypatch.setattr(entrypoint.subprocess, "run", run)

    runtime_dir = entrypoint._ensure_dedicated_tmux_tmpdir(tmp_path, sessions_dir)

    commands = [call.args[0] for call in run.call_args_list]
    assert all("TMUX" not in call.kwargs["env"] for call in run.call_args_list)
    assert ["tmux", "kill-session", "-t", "=cc-1-0"] in commands
    assert not any(command == ["tmux", "kill-session", "-t", "=unrelated"] for command in commands)
    assert ["tmux", "set-environment", "-gu", "TELEGRAM_BOT_TOKEN"] in commands
    assert (runtime_dir / ".legacy-default-migrated").read_text() == "migrated\n"


def test_public_settings_default_cwd_is_generic(monkeypatch) -> None:
    for name in ("BOT_LANG", "PROJECT_ROOT", "DEFAULT_CWD", "TOPIC_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None, telegram_bot_token="test-token")

    assert settings.bot_lang == "en"
    assert settings.project_root == "."
    assert settings.default_cwd == "."
    assert settings.topic_config_path == "./topic_config.json"


def test_public_dotenv_values_are_not_interpolated(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TELEGRAM_BOT_TOKEN=token-${SHOULD_NOT_EXPAND} # deployment\n"
        'DEEPGRAM_API_KEY="key-$ALSO_LITERAL"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOULD_NOT_EXPAND", "secret")
    monkeypatch.setenv("ALSO_LITERAL", "secret")

    exact = read_exact_env_file(env_path)
    settings = Settings(_env_file=env_path)

    assert exact["TELEGRAM_BOT_TOKEN"] == "token-${SHOULD_NOT_EXPAND}"
    assert settings.telegram_bot_token == "token-${SHOULD_NOT_EXPAND}"
    assert settings.deepgram_api_key == "key-$ALSO_LITERAL"


def test_public_tmux_server_environment_drops_bot_secrets(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("TMUX_TMPDIR", "/run/public-bot-tmux")
    run = MagicMock(
        side_effect=[
            MagicMock(returncode=0, stdout="TELEGRAM_BOT_TOKEN=secret\n"),
            MagicMock(returncode=0, stdout=""),
        ]
    )

    env = sanitized_tmux_environment(run=run)

    assert "TELEGRAM_BOT_TOKEN" not in env
    assert env["TMUX_TMPDIR"] == "/run/public-bot-tmux"
    assert run.call_args_list[1].args[0] == [
        "tmux",
        "set-environment",
        "-gu",
        "TELEGRAM_BOT_TOKEN",
    ]


def test_public_detects_legacy_tmux_pane_with_service_secret(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: b"TELEGRAM_BOT_TOKEN=secret\0")
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="123\n"))

    assert tmux_pane_inherits_disallowed_environment("cc-1-0", run=run) is True


def test_public_settings_support_split_app_and_workspace_roots(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    workspace_root = tmp_path / "workspace"
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        project_root=str(tmp_path / "legacy"),
        app_root=str(app_root),
        agent_workspace_root=str(workspace_root),
    )

    assert settings.app_root_path == app_root
    assert settings.workspace_root_path == workspace_root
    assert settings.resolve_app_path("config.json") == app_root / "config.json"
    assert settings.resolve_workspace_path("topic_config.json") == (
        workspace_root / "topic_config.json"
    )


def test_public_relative_file_cache_dir_is_agent_readable(tmp_path: Path) -> None:
    root = tmp_path / "bot"
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        project_root=str(root),
        default_cwd=".",
        file_cache_dir="./data",
    )
    session_manager = SessionManager(settings)

    assert session_manager.file_cache_dir == str((root / "data").resolve())


def test_public_start_wires_live_buffer_before_restore_all() -> None:
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")

    assert "tmux_manager.wire_live_buffer(bot=bot, topic_config=topic_config)" in source
    assert source.index("tmux_manager.wire_live_buffer") < source.index("tmux_manager.restore_all")
    assert "tmux_manager.restore_all(session_manager)" in source


def test_public_start_wires_codex_update_and_tail_runtime() -> None:
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")

    assert "codex_update_service = CodexUpdateService(" in source
    assert "tmux_manager.wire_codex_update_service(codex_update_service)" in source
    assert 'dp["codex_update_service"] = codex_update_service' in source
    assert "ForwardBatcher(bot=bot, transcriber=transcriber)" in source
    assert "dp.include_router(tail_router)" in source
    assert source.index("dp.include_router(tail_router)") < source.index(
        "dp.include_router(text_router)"
    )
    assert "recovery_factory = make_recovery_on_event(" in source
    assert "await tmux_manager.resume_tails(recovery_factory)" in source
    assert "tmux_manager.start_modal_watchdog()" in source
    assert "tmux_manager.start_transcript_watchdog()" in source
    assert "await tmux_manager.stop_transcript_watchdog()" in source
    assert "await tmux_manager.stop_modal_watchdog()" in source
    assert "picker_store = PickerStore()" in source
    assert "bot_defaults = BotDefaults(" in source
    assert 'dp["picker_store"] = picker_store' in source
    assert 'dp["bot_defaults"] = bot_defaults' in source
    assert source.index("await tmux_manager.resume_tails(recovery_factory)") < source.index(
        "await dp.start_polling"
    )


def test_public_entrypoint_uses_workspace_root_for_runtime_state() -> None:
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")

    assert "settings.resolve_workspace_path(settings.topic_config_path)" in source
    assert "settings.resolve_workspace_path(settings.tmux_sessions_dir)" in source
    assert "settings.resolve_workspace_path(settings.default_cwd)" in source
    assert "TopicConfig(str(topic_config_path), str(workspace_root))" in source
    assert "TmuxManager(\n        sessions_dir=tmux_sessions_dir" in source
    assert 'state_path=tmux_sessions_dir / "codex_update.json"' in source
    assert "cwd=settings.resolve_workspace_path(settings.default_cwd)" in source


def test_public_prompt_modes_are_available() -> None:
    prompts_dir = Path("src/telegram_bot/prompts")
    assert {path.name for path in prompts_dir.glob("*.md")} == {
        "default.md",
        "task-manager.md",
    }
    for mode in ("free", "task"):
        assert cc_modes._get_mode_prompt(mode)
    assert cc_modes._get_mode_prompt("task") == (prompts_dir / "task-manager.md").read_text()
    assert set(cc_modes._MODE_TOOLS) == {"free", "task"}


def test_public_runtime_rejects_unregistered_prompt_mode(tmp_path: Path) -> None:
    runtime = resolve_topic_runtime_config(
        TopicSettings(
            name="Private",
            type="project",
            mode="private",
            cwd=None,
            mcp_config=None,
        ),
        BotDefaults(cwd=tmp_path, mcp_config=tmp_path / ".mcp.json"),
    )

    assert runtime.mode == "free"


def test_public_agent_environment_is_sanitized() -> None:
    env = agent_process_env(
        base_env={
            "HOME": "/home/test",
            "PATH": "/usr/bin",
            "APP_ROOT": "/srv/bot",
            "AGENT_WORKSPACE_ROOT": "/srv/workspace",
            "PROJECT_ROOT": "/srv/legacy",
            "TELEGRAM_BOT_TOKEN": "must-not-leak",
            "DEEPGRAM_API_KEY": "must-not-leak",
            "UNRELATED_SECRET": "must-not-leak",
        }
    )

    assert env["APP_ROOT"] == "/srv/bot"
    assert env["AGENT_WORKSPACE_ROOT"] == "/srv/workspace"
    assert env["PROJECT_ROOT"] == "/srv/legacy"
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "DEEPGRAM_API_KEY" not in env
    assert "UNRELATED_SECRET" not in env


def test_public_prompt_modes_have_bot_mcp_tools() -> None:
    required = {
        "mcp__bot__send_message",
        "mcp__bot__send_image",
        "mcp__bot__send_image_gallery",
        "mcp__bot__send_document",
    }

    for mode in ("free", "task"):
        tools = set(cc_modes._MODE_TOOLS[mode].split(","))
        assert required <= tools
        assert "mcp__bot__send_file" not in tools


def test_free_mode_allows_skill_for_topic_setup() -> None:
    tools = set(cc_modes._MODE_TOOLS["free"].split(","))

    assert "Skill" in tools


def test_public_prompt_modes_allow_context7_docs_tools() -> None:
    required = {
        "mcp__context7__resolve-library-id",
        "mcp__context7__query-docs",
        "mcp__context7__get-library-docs",
    }

    for mode in ("free", "task"):
        tools = set(cc_modes._MODE_TOOLS[mode].split(","))
        assert required <= tools


def test_engine_selection_falls_back_to_available_cli(monkeypatch) -> None:
    monkeypatch.setattr(
        "telegram_bot.core.services.providers.is_engine_available",
        lambda engine: engine == "codex",
    )

    assert choose_available_engine("claude") == "codex"


def test_topic_config_parses_public_runtime_fields(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    mcp_config = project / ".mcp.json"
    mcp_config.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "42": {
                        "name": "Demo",
                        "type": "project",
                        "mode": "free",
                        "cwd": str(project),
                        "mcp_config": str(mcp_config),
                        "stream_mode": "minimal",
                        "exec_mode": "tmux",
                        "engine": "codex",
                        "model": "legacy-model",
                        "models": {
                            "claude": "claude-model",
                            "codex": "codex-model",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    topic = TopicConfig(str(config_path), ".").get_topic(42)

    assert topic.name == "Demo"
    assert topic.mode == "free"
    assert topic.cwd == str(project)
    assert topic.mcp_config == str(mcp_config)
    assert topic.stream_mode == "minimal"
    assert topic.exec_mode == "tmux"
    assert topic.engine == "codex"
    assert topic.model == "legacy-model"
    assert topic.models == {
        "claude": "claude-model",
        "codex": "codex-model",
    }


def test_topic_config_normalizes_and_filters_model_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "1": {
                        "mode": "free",
                        "model": "  legacy-model  ",
                        "models": {
                            "claude": "  claude-model  ",
                            "codex": "bad model with spaces",
                            "unknown": "private-model",
                        },
                    },
                    "2": {
                        "mode": "free",
                        "model": {"invalid": "type"},
                        "models": ["invalid", "type"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    config = TopicConfig(str(config_path), ".")

    first = config.get_topic(1)
    second = config.get_topic(2)

    assert first.model == "legacy-model"
    assert first.models == {"claude": "claude-model"}
    assert second.model is None
    assert second.models == {}


def test_codex_provider_parser_smoke() -> None:
    parsed = CODEX_ADAPTER.parse_exec_event('{"type":"thread.started","thread_id":"abc"}')

    assert parsed.session_id == "abc"
    assert parsed.events == []


def test_codex_0147_items_keep_clarification_in_the_same_turn() -> None:
    parser = CodexTranscriptParser()
    records = [
        '{"type":"turn_context","payload":{"turn_id":"turn-147"}}',
        (
            '{"type":"event_msg","payload":{"type":"item_completed","item":'
            '{"type":"AgentMessage","phase":"commentary","content":'
            '[{"type":"Text","text":"working"}]}}}'
        ),
        (
            '{"type":"event_msg","payload":{"type":"item_completed","item":'
            '{"type":"AgentMessage","phase":"final_answer","content":'
            '[{"type":"Text","text":"first answer"}]}}}'
        ),
        (
            '{"type":"event_msg","payload":{"type":"item_completed","item":'
            '{"type":"UserMessage","content":[{"type":"text","text":"clarify"}]}}}'
        ),
        (
            '{"type":"event_msg","payload":{"type":"item_completed","item":'
            '{"type":"AgentMessage","phase":"final_answer","content":'
            '[{"type":"Text","text":"second answer"}]}}}'
        ),
        '{"type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-147"}}',
    ]

    events = [event for raw in records for event in parser.parse(raw).events]

    assert [(event.type, event.content) for event in events] == [
        ("turn_start", ""),
        ("text", "working"),
        ("result_message", "first answer"),
        ("result_message", "second answer"),
        ("turn_end", ""),
    ]
    assert {event.turn_id for event in events} == {"turn-147"}


def test_claude_local_command_records_do_not_open_a_turn() -> None:
    parser = ClaudeTranscriptParser()
    boundary_parser = ClaudeTranscriptParser()
    records = [
        (
            "<local-command-caveat>Local command metadata.</local-command-caveat>",
            True,
        ),
        (
            "<command-name>/model</command-name>\n"
            "<command-message>model</command-message>\n"
            "<command-args></command-args>",
            None,
        ),
        ("<local-command-stdout>Set model to Opus.</local-command-stdout>", None),
    ]

    for content, is_meta in records:
        record: dict[str, object] = {
            "type": "user",
            "promptId": "local-command",
            "message": {"role": "user", "content": content},
        }
        if is_meta is not None:
            record["isMeta"] = is_meta
        raw = json.dumps(record)
        assert parser.parse(raw)[0] == []
        assert boundary_parser.is_turn_boundary(raw) is False

    assert parser.current_turn_id is None


def test_public_command_handlers_are_wired() -> None:
    assert commands.handle_resume is not None
    assert commands.handle_stream_mode is not None
    assert commands.handle_mode_command is not None
    assert commands.handle_engine_command is not None
    assert commands.handle_recycle is not None
    assert commands.handle_mcpstatus is not None
    assert handle_tail_command is not None


def test_public_bot_command_menu_is_public_only() -> None:
    command_names = {command.command for command in build_bot_commands("ru")}

    assert "clear" in command_names
    assert "codex_update" in command_names
    assert "recycle" in command_names
    assert "mcpstatus" in command_names
    assert "tui" in command_names
    assert "tail" in command_names
    assert "new" not in command_names
    assert "day" not in command_names


def test_public_start_registers_bot_commands() -> None:
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")

    assert "setup_bot_commands(bot)" in source
    assert source.index("setup_bot_commands(bot)") < source.index("dp.start_polling")
    assert "_stop_polling_when_started(dp)" in source


def test_mcp_bot_server_imports() -> None:
    path = Path("mcp-servers/bot/server.py")
    spec = importlib.util.spec_from_file_location("public_bot_mcp_server", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "send_message")
    assert hasattr(module, "send_image")
    assert hasattr(module, "send_image_gallery")
    assert hasattr(module, "send_document")
    assert not hasattr(module, "send_file")

    assert module._normalize_parse_mode("html") == ("HTML", None)
    assert module._normalize_parse_mode("MarkdownV2") == ("MarkdownV2", None)
    invalid_mode, error = module._normalize_parse_mode("Markdown")
    assert invalid_mode is None
    assert error is not None


def test_public_rich_final_answer_detection_requires_table() -> None:
    plain = detect_rich_send("Final answer without a table.")
    table = detect_rich_send("| Feature | Status |\n| --- | --- |\n| Tables | work |")

    assert not plain.eligible
    assert plain.reason == "plain-no-rich-structure"
    assert table.eligible
    assert table.input_rich_message is not None


def test_public_rich_sender_falls_back_when_ordered_list_restarts() -> None:
    item = "   Context line for the plan item.\n\n"
    contacts = "Contacts:\n\n" + "".join(f"{n}. Contact {n}\n{item}" for n in range(1, 4))
    tasks = "Tasks:\n\n" + "".join(f"{n}. Task {n}\n{item}" for n in range(4, 8))
    text = contacts + tasks + ("body " * 900)

    decision = detect_rich_send(text)

    assert decision.eligible is False
    assert decision.reason == "ordered-list-restart"
    assert decision.fallback_text == text
