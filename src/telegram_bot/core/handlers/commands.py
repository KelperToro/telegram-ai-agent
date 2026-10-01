"""Command handlers for bot-owned slash commands except /tui and /tail."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import math
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.types.inaccessible_message import InaccessibleMessage

from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.keyboards import (
    RESUME_PAGE_SIZE,
    _format_age,
    engine_keyboard,
    exec_mode_keyboard,
    resume_keyboard,
    stream_mode_keyboard,
    topic_keyboard,
)
from telegram_bot.core.messages import reset_lang_cache, t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.codex_app_server import (
    CodexAppServerError,
    clear_thread_goal,
    get_thread_goal,
    list_codex_thread_titles,
    set_thread_goal,
)
from telegram_bot.core.services.codex_update import CodexUpdateResult, CodexUpdateService
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.picker_store import PickerState, PickerStore
from telegram_bot.core.services.providers import engine_display_name, is_engine_available
from telegram_bot.core.services.resume_listing import (
    SessionEntry,
    _same_cwd,
    get_last_assistant_message,
    list_sessions,
)
from telegram_bot.core.services.telegram_utils import send_html_with_fallback
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import (
    _VALID_ENGINES,
    _VALID_EXEC_MODES,
    _VALID_STREAM_MODES,
    Engine,
    TopicConfig,
    config_id_for_channel,
)
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config
from telegram_bot.core.types import ChannelKey, channel_key
from telegram_bot.core.utils.telegram_html import split_html_message

logger = logging.getLogger(__name__)


def _exec_mode_label(mode: str) -> str:
    """Human-facing label per exec_mode.

    Raw "subprocess" must never leak into the "Mode: …" toast — the picker
    button text is the contract surface.
    """
    if mode == "subprocess":
        return t("ui.exec_mode_label_subprocess")
    if mode == "tmux":
        return "TUI (ConPTY)" if os.name == "nt" else t("ui.exec_mode_label_tmux")
    return mode


def _exec_mode_picker_caption(mode: str) -> str:
    return t("ui.exec_mode_picker_caption", current=_exec_mode_label(mode))


router = Router(name="commands")


def _format_codex_update_time(value: float | None) -> str:
    if value is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value))


def _format_codex_update_output(output: str) -> str:
    return html.escape(output or "No output")


def _codex_update_active_check(
    tmux_manager: TmuxManager,
    session_manager: SessionManager,
) -> bool:
    return tmux_manager.has_live_provider("codex") or session_manager.has_active_provider_process(
        "codex"
    )


def _codex_update_result_message(result: CodexUpdateResult) -> str:
    if result.status == "success":
        return t("ui.codex_update_success", output=_format_codex_update_output(result.output))
    if result.status == "already_running":
        return t("ui.codex_update_already_running")
    if result.status == "blocked_active_sessions":
        return t("ui.codex_update_active_sessions")
    if result.status == "skipped_cooldown":
        return t("ui.codex_update_cooldown")
    return t(
        "ui.codex_update_failed",
        status=result.status,
        output=_format_codex_update_output(result.output),
    )


def _resume_caption(
    cwd: Path,
    *,
    page: int,
    total_pages: int,
    entries: tuple[SessionEntry, ...] = (),
    current_session_id: str | None = None,
    all_projects: bool = False,
) -> str:
    header = "ui.resume_picker_caption_all_hdr" if all_projects else "ui.resume_picker_caption_hdr"
    text = t(header, cwd=html.escape(str(cwd)), page=page + 1, total=total_pages)
    if not entries:
        return text

    blocks: list[str] = []
    start = page * RESUME_PAGE_SIZE
    for idx, entry in enumerate(entries[start : start + RESUME_PAGE_SIZE], start=start):
        provider = engine_display_name(entry.provider)
        label = entry.title or entry.preview
        if label == entry.session_id[:8] and entry.cwd is not None:
            label = f"{entry.cwd.name} · {label}"
        preview = html.escape(label)
        prefix = "✅ " if entry.session_id == current_session_id else ""
        parts = [f"{prefix}{idx + 1}. <b>{preview}</b>"]
        details = [provider, _format_age(entry.mtime)]
        if entry.cwd is not None and all_projects:
            details.append("📁 " + html.escape(entry.cwd.name))
        details.append(html.escape(entry.session_id[:8]))
        if entry.session_id == current_session_id:
            details.append(t("ui.resume_current_marker"))
        block_lines = ["".join(parts), "   " + " · ".join(details)]
        blocks.append("\n".join(block_lines))
    return "\n\n".join([text, *blocks])


def _pin_current_session(
    entries: tuple[SessionEntry, ...], current_session_id: str | None
) -> tuple[SessionEntry, ...]:
    if current_session_id is None:
        return entries
    for index, entry in enumerate(entries):
        if entry.session_id == current_session_id:
            return (entry, *entries[:index], *entries[index + 1 :])
    return entries


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    logger.debug("User %s started the bot", message.from_user and message.from_user.id)
    is_group = message.chat.type == ChatType.SUPERGROUP
    keyboard = topic_keyboard() if is_group else ReplyKeyboardRemove()
    await message.answer(
        text=t("ui.start_welcome"),
        reply_markup=keyboard,
    )


@router.message(Command("language"))
async def handle_language(message: Message) -> None:
    """Show or switch bot UI language for the current process."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    current = os.environ.get("BOT_LANG", "en")
    if current not in {"en", "ru"}:
        current = "en"

    if len(parts) == 1:
        await message.answer(t("ui.language_current", lang=current))
        return

    lang = parts[1].strip().lower()
    if lang not in {"en", "ru"}:
        await message.answer(t("ui.language_invalid"))
        return

    os.environ["BOT_LANG"] = lang
    reset_lang_cache()
    await message.answer(t("ui.language_changed", lang=lang))


@router.message(Command("codex_update"))
async def handle_codex_update(
    message: Message,
    codex_update_service: CodexUpdateService,
    tmux_manager: TmuxManager,
    session_manager: SessionManager,
) -> None:
    """Run or inspect the bot-managed Codex CLI updater."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lower() == "status":
        state = codex_update_service.status()
        output = f"<pre>{_format_codex_update_output(state.last_output)}</pre>"
        await message.answer(
            t(
                "ui.codex_update_status",
                status=state.last_status or "never",
                last_success=_format_codex_update_time(state.last_success_at),
                output=output,
            ),
            parse_mode="HTML",
        )
        return

    running = await message.answer(t("ui.codex_update_running"))
    result = await codex_update_service.run_manual(
        active_check=lambda: _codex_update_active_check(tmux_manager, session_manager)
    )
    response = _codex_update_result_message(result)
    with contextlib.suppress(TelegramBadRequest):
        await running.edit_text(response, parse_mode="HTML")
        return
    await message.answer(response, parse_mode="HTML")


@router.message(Command("goal"))
async def handle_goal(
    message: Message,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
) -> None:
    """Manage the selected Codex thread's persisted goal through app-server."""
    key = channel_key(message)
    session_id = (
        tmux_manager.get_active_session_id(key) or session_manager._get_session(key).session_id
    )
    if not session_id:
        await message.answer(t("ui.goal_no_session"))
        return
    try:
        is_codex = uuid.UUID(session_id).version == 7
    except ValueError:
        is_codex = False
    if not is_codex:
        await message.answer(t("ui.goal_codex_only"))
        return

    raw = (message.text or "").partition(" ")[2].strip()
    action, _, value = raw.partition(" ")
    try:
        if not raw or action == "status":
            goal = await get_thread_goal(session_id)
        elif action == "set" and value.strip():
            goal = await set_thread_goal(session_id, objective=value.strip(), status="active")
        elif action in {"pause", "resume", "complete"} and not value.strip():
            status = {"pause": "paused", "resume": "active", "complete": "complete"}[action]
            goal = await set_thread_goal(session_id, status=status)
        elif action == "budget" and value.strip().isdigit():
            goal = await set_thread_goal(session_id, token_budget=int(value.strip()))
        elif action == "clear" and not value.strip():
            cleared = await clear_thread_goal(session_id)
            await message.answer(t("ui.goal_cleared" if cleared else "ui.goal_missing"))
            return
        else:
            await message.answer(t("ui.goal_help"))
            return
    except (CodexAppServerError, ValueError):
        logger.warning("Codex goal command failed for channel %s", key, exc_info=True)
        await message.answer(t("ui.goal_failed"))
        return

    if goal is None:
        await message.answer(t("ui.goal_missing"))
        return
    objective = html.escape(str(goal.get("objective", "")))
    status = html.escape(str(goal.get("status", "unknown")))
    used = goal.get("tokensUsed", 0)
    budget = goal.get("tokenBudget")
    await message.answer(
        t(
            "ui.goal_status",
            objective=objective,
            status=status,
            used=used,
            budget=budget if budget is not None else "∞",
        ),
        parse_mode="HTML",
    )


async def _reset_channel(
    message: Message,
    key: ChannelKey,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    """Unified reset path for /new, /clear, and the "Новый чат" reply button.

    Live tmux → clear_context respawns a fresh TUI immediately.
    Dormant tmux → drop stale state and start a fresh TUI immediately.
    Otherwise → full subprocess reset + ui.new_session.
    """
    settings = topic_config.get_topic(config_id_for_channel(key))
    if tmux_manager.is_active(key):
        # clear_context respawns the tmux session; _spawn_tmux can fail
        # (tmux server shutdown race, readiness timeout, etc.). Without a
        # catch here the RuntimeError reaches aiogram's error middleware
        # and the user sees nothing — "Новый чат" becomes a silent button.
        try:
            reset_live = await tmux_manager.clear_context(key, session_manager)
        except RuntimeError:
            logger.warning("clear_context failed for %s", key, exc_info=True)
            await message.answer(t("ui.reset_failed"))
            return
        if reset_live:
            session = session_manager._get_session(key)
            await message.answer(
                t("ui.tmux_started_engine", engine=engine_display_name(session.engine))
            )
            return
        logger.info("clear_context found no live tmux for %s; starting fresh", key)

    if settings.exec_mode == "tmux":
        await tmux_manager.kill(key)
        forward_batcher.clear(key)
        await message_queue.clear(key)
        await session_manager.kill_session(key)
        session = session_manager._get_session(key)
        try:
            started = await tmux_manager.start_session(
                key,
                mode=session.mode,
                cwd=session.cwd,
                mcp_config=session.mcp_config,
                chat_id=session.chat_id,
                session_manager=session_manager,
                provider=session.engine,
                model=session.model,
            )
        except RuntimeError:
            logger.warning("fresh tmux start failed for %s", key, exc_info=True)
            await message.answer(t("ui.reset_failed"))
            return
        if started:
            await message.answer(
                t("ui.tmux_started_engine", engine=engine_display_name(session.engine))
            )
        return

    forward_batcher.clear(key)
    await message_queue.clear(key)
    await session_manager.kill_session(key)
    await message.answer(t("ui.new_session"))


@router.message(Command("new"))
async def handle_new(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    logger.debug("User %s requested new session", message.from_user and message.from_user.id)
    await _reset_channel(
        message, key, session_manager, message_queue, forward_batcher, tmux_manager, topic_config
    )


@router.message(Command("clear"))
async def handle_clear(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    logger.debug("User %s requested clear", message.from_user and message.from_user.id)
    await _reset_channel(
        message, key, session_manager, message_queue, forward_batcher, tmux_manager, topic_config
    )


@router.message(Command("cancel"))
async def handle_cancel_command(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
) -> None:
    key = channel_key(message)
    tmux_acted = tmux_manager.is_active(key)
    if tmux_acted:
        await tmux_manager.cancel(key)
    cancelled = await message_queue.cancel(key)
    if cancelled or tmux_acted:
        logger.debug("User cancelled CC processing (command) for %s", key)
        await message.answer(t("ui.cancelled"))
    else:
        await message.answer(t("ui.nothing_to_cancel"))


@router.message(Command("kill"))
async def handle_kill(
    message: Message,
    tmux_manager: TmuxManager,
    session_manager: SessionManager,
    message_queue: MessageQueue,
) -> None:
    """Kill the active runtime and forget its conversation."""
    key = channel_key(message)
    if not tmux_manager.is_active(key):
        await message_queue.clear(key)
        await session_manager.kill_session(key)
        await message.answer(t("ui.subprocess_killed"))
        return
    logger.debug(
        "User %s killed tmux session for %s", message.from_user and message.from_user.id, key
    )
    await tmux_manager.kill(key)
    await message.answer(t("ui.tmux_killed"))


@router.message(Command("mcpstatus"))
async def handle_mcpstatus(message: Message, tmux_manager: TmuxManager) -> None:
    """Show redacted MCP process diagnostics for the current topic."""
    key = channel_key(message)
    status = html.escape(tmux_manager.mcp_status_text(key))
    await message.answer(f"<pre>{status}</pre>", parse_mode="HTML")


@router.message(Command("recycle"))
async def handle_recycle(
    message: Message,
    tmux_manager: TmuxManager,
    session_manager: SessionManager,
    message_queue: MessageQueue,
) -> None:
    """Restart the current tmux runtime without intentionally clearing context."""
    key = channel_key(message)
    if not tmux_manager.is_active(key):
        await message_queue.cancel(key)
        await message.answer(t("ui.subprocess_recycled"))
        return
    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await message.answer(t("ui.exec_mode_busy"))
        return
    try:
        ok = await tmux_manager.recycle(key, session_manager)
    except RuntimeError:
        logger.warning("recycle failed for %s", key, exc_info=True)
        await message.answer(t("ui.recycle_failed"))
        return
    if ok:
        await message.answer(t("ui.recycle_done"))
    else:
        await message.answer(t("ui.tmux_not_active"))


@router.message(Command("resume"))
async def handle_resume(
    message: Message,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    picker_store: PickerStore,
    bot_defaults: BotDefaults,
) -> None:
    """Open server-side picker with resumable Claude/Codex sessions."""
    key = channel_key(message)
    runtime = resolve_topic_runtime_config(
        topic_config.get_topic(config_id_for_channel(key)), bot_defaults
    )
    private_chat = key[0] > 0 and key[1] is None
    entries = tuple(await asyncio.to_thread(list_sessions, runtime.cwd, all_codex=private_chat))
    if private_chat:
        try:
            names = await list_codex_thread_titles(
                fallback_ids=(entry.session_id for entry in entries if entry.provider == "codex")
            )
        except (CodexAppServerError, OSError, TimeoutError):
            logger.warning("Could not load Codex thread names", exc_info=True)
            names = {}
        entries = tuple(
            replace(entry, title=names.get(entry.session_id))
            if entry.provider == "codex"
            else entry
            for entry in entries
        )
    query = (message.text or "").partition(" ")[2].strip().casefold()
    if query:
        entries = tuple(
            entry
            for entry in entries
            if query
            in " ".join(
                (entry.title or "", entry.preview, str(entry.cwd or ""), entry.session_id)
            ).casefold()
        )
    if not entries:
        await message.answer(t("ui.resume_no_matches" if query else "ui.resume_no_sessions"))
        return

    current_session_id = tmux_manager.get_active_session_id(
        key
    ) or session_manager.get_current_session_id(key)
    entries = _pin_current_session(entries, current_session_id)
    token = picker_store.put(
        PickerState(
            chat_id=key[0],
            thread_id=key[1],
            cwd=runtime.cwd,
            engine=runtime.engine,
            entries=entries,
            created_at=time.time(),
        )
    )
    total_pages = max(1, math.ceil(len(entries) / 8))
    await message.answer(
        _resume_caption(
            runtime.cwd,
            page=0,
            total_pages=total_pages,
            entries=entries,
            current_session_id=current_session_id,
            all_projects=private_chat,
        ),
        reply_markup=resume_keyboard(
            entries,
            page=0,
            current_session_id=current_session_id,
            token=token,
        ),
        parse_mode="HTML",
    )


def _callback_key(callback: CallbackQuery) -> ChannelKey | None:
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        return None
    return (callback.message.chat.id, callback.message.message_thread_id)


async def _stale_resume_picker(callback: CallbackQuery) -> None:
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(t("ui.resume_picker_stale"), reply_markup=None)
    await callback.answer(t("ui.resume_picker_stale"), show_alert=True)


async def _answer_callback_safely(
    callback: CallbackQuery, text: str | None = None, *, show_alert: bool = False
) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer(text, show_alert=show_alert)


async def _replay_last_assistant_message(
    message: Message,
    entry: SessionEntry,
    key: ChannelKey,
    session_manager: SessionManager,
) -> None:
    content = await asyncio.to_thread(
        get_last_assistant_message,
        entry.provider,
        entry.transcript_path,
    )
    if not content:
        return

    for chunk in split_html_message(content):

        async def _send_html(c: str = chunk) -> object:
            return await message.answer(c, parse_mode="HTML")

        async def _send_plain(c: str = chunk) -> object:
            return await message.answer(c)

        outcome = await send_html_with_fallback(
            send_html=_send_html,
            send_plain=_send_plain,
            label=f"resume replay {key}",
        )
        if outcome.message_id is not None:
            session_manager.record_message(
                outcome.message_id,
                entry.session_id,
                key,
                provider=entry.provider,
                model=None,
            )
        if outcome.fatal:
            return


@router.callback_query(F.data.startswith("rs:p:"))
async def on_resume_page(
    callback: CallbackQuery,
    picker_store: PickerStore,
    tmux_manager: TmuxManager,
    session_manager: SessionManager,
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _stale_resume_picker(callback)
        return
    _, _, token, raw_page = parts
    state = picker_store.get(token)
    key = _callback_key(callback)
    if state is None or key != (state.chat_id, state.thread_id):
        await _stale_resume_picker(callback)
        return
    try:
        page = int(raw_page)
    except ValueError:
        await _stale_resume_picker(callback)
        return
    total_pages = max(1, math.ceil(len(state.entries) / 8))
    page = max(0, min(page, total_pages - 1))
    try:
        await callback.message.edit_text(
            _resume_caption(
                state.cwd,
                page=page,
                total_pages=total_pages,
                entries=state.entries,
                current_session_id=tmux_manager.get_active_session_id(key)
                or session_manager.get_current_session_id(key),
                all_projects=key[0] > 0 and key[1] is None,
            ),
            reply_markup=resume_keyboard(
                state.entries,
                page=page,
                current_session_id=tmux_manager.get_active_session_id(key)
                or session_manager.get_current_session_id(key),
                token=token,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("rs:s:"))
async def on_resume_pick(
    callback: CallbackQuery,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    picker_store: PickerStore,
    bot_defaults: BotDefaults,
    message_queue: MessageQueue | None = None,
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _stale_resume_picker(callback)
        return
    _, _, token, raw_idx = parts
    state = picker_store.get(token)
    key = _callback_key(callback)
    if state is None or key != (state.chat_id, state.thread_id):
        await _stale_resume_picker(callback)
        return
    runtime = resolve_topic_runtime_config(
        topic_config.get_topic(config_id_for_channel(key)), bot_defaults
    )
    if not _same_cwd(runtime.cwd, state.cwd):
        await _stale_resume_picker(callback)
        return
    try:
        idx = int(raw_idx)
    except ValueError:
        await _stale_resume_picker(callback)
        return
    if idx < 0:
        await _stale_resume_picker(callback)
        return
    try:
        entry = state.entries[idx]
    except IndexError:
        await _stale_resume_picker(callback)
        return

    target_cwd = entry.cwd or state.cwd
    if not entry.transcript_path.is_file():
        await callback.message.edit_text(t("ui.resume_target_missing"), reply_markup=None)
        return
    config_id = config_id_for_channel(key)
    if not _same_cwd(target_cwd, state.cwd):
        if config_id is None or not await topic_config.update_engine_cwd(
            config_id, entry.provider, target_cwd
        ):
            await callback.message.edit_text(t("ui.resume_config_write_failed"), reply_markup=None)
            return
        runtime = resolve_topic_runtime_config(topic_config.get_topic(config_id), bot_defaults)

    if runtime.exec_mode == "subprocess":
        if not is_engine_available(entry.provider):
            await callback.answer(t("ui.agent_cli_not_found"), show_alert=True)
            return
        if message_queue is not None and message_queue.is_busy(key):
            await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
            return
        if not entry.transcript_path.is_file():
            await callback.message.edit_text(t("ui.resume_target_missing"), reply_markup=None)
            return
        current_sid = session_manager.get_current_session_id(key)
        if (
            config_id is not None
            and runtime.engine != entry.provider
            and not await topic_config.update_engine(config_id, entry.provider)
        ):
            await callback.message.edit_text(t("ui.resume_config_write_failed"), reply_markup=None)
            return
        await session_manager.override_session(key, entry.session_id, provider=entry.provider)
        session_manager.save_mapping()
        picker_store.drop(token)
        message_key = (
            "ui.resume_already_on_it" if current_sid == entry.session_id else "ui.resume_switched"
        )
        selection = (
            t(message_key, sid=entry.session_id[:8])
            + "\n"
            + t(
                "ui.resume_selection_details",
                title=html.escape(entry.title or entry.preview),
                cwd=html.escape(str(target_cwd)),
            )
        )
        await callback.message.edit_text(selection, reply_markup=None, parse_mode="HTML")
        await _replay_last_assistant_message(callback.message, entry, key, session_manager)
        return

    await _answer_callback_safely(callback, t("ui.resume_starting"))
    result = await tmux_manager.switch_or_start_session(
        key,
        entry.session_id,
        entry.provider,
        entry.transcript_path,
        session_manager=session_manager,
        topic_config=topic_config,
        defaults=bot_defaults,
    )
    if result.kind == "target_missing":
        await callback.message.edit_text(t("ui.resume_target_missing"), reply_markup=None)
        return
    if result.kind in {"invalid_id", "spawn_failed", "config_write_failed"}:
        key_name = (
            "ui.resume_spawn_failed_engine_changed"
            if result.kind == "spawn_failed" and result.engine_changed
            else f"ui.resume_{result.kind}"
        )
        await callback.message.edit_text(
            t(key_name, engine=entry.provider),
            reply_markup=None,
        )
        return

    picker_store.drop(token)
    if result.kind == "already_on_it":
        await callback.message.edit_text(t("ui.resume_already_on_it"), reply_markup=None)
        await _replay_last_assistant_message(callback.message, entry, key, session_manager)
        return

    message_key = "ui.resume_switched" if result.kind == "switched" else "ui.resume_started"
    text = t(message_key, sid=entry.session_id[:8])
    if result.engine_changed:
        text += "\n" + t("ui.resume_engine_switched", engine=entry.provider)
    await callback.message.edit_text(text, reply_markup=None, parse_mode="HTML")
    await _replay_last_assistant_message(callback.message, entry, key, session_manager)


@router.callback_query(F.data.startswith("rs:cancel:"))
async def on_resume_cancel(callback: CallbackQuery, picker_store: PickerStore) -> None:
    if callback.data is not None:
        picker_store.drop(callback.data.rsplit(":", 1)[-1])
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(t("ui.resume_cancelled"), reply_markup=None)
    await callback.answer()


@router.message(Command("stream"))
async def handle_stream_mode(message: Message, topic_config: TopicConfig) -> None:
    """Show a 3-button picker to switch stream_mode for the current topic."""
    config_id = config_id_for_channel(channel_key(message))
    if config_id is None:
        await message.answer(t("ui.stream_mode_not_in_forum"))
        return
    current = topic_config.get_topic(config_id).stream_mode
    await message.answer(
        t("ui.stream_mode_picker_caption", current=current),
        reply_markup=stream_mode_keyboard(current),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("stream_mode:"))
async def on_stream_mode_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager | None = None,
) -> None:
    """Apply a new stream_mode for the topic the picker was posted in."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    # InaccessibleMessage has no thread_id/edit methods — bail out if the
    # picker message is no longer reachable (e.g. deleted, chat lost).
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    _, _, mode = callback.data.partition(":")
    if mode not in _VALID_STREAM_MODES:
        await callback.answer(t("ui.stream_mode_invalid"), show_alert=True)
        return

    key = (callback.message.chat.id, callback.message.message_thread_id)
    config_id = config_id_for_channel(key)
    if config_id is None:
        await callback.answer(
            t("ui.stream_mode_not_in_forum"),
            show_alert=True,
        )
        return

    previous_mode = topic_config.get_topic(config_id).stream_mode
    ok = await topic_config.update_stream_mode(config_id, mode)  # type: ignore[arg-type]
    if not ok:
        await callback.answer(t("ui.stream_mode_write_failed"), show_alert=True)
        return
    if previous_mode == "live" and mode != "live" and tmux_manager is not None:
        await tmux_manager.close_buffer(
            key,
        )

    # Refresh both caption and keyboard so the visible current value matches the checkmark.
    try:
        await callback.message.edit_text(
            t("ui.stream_mode_picker_caption", current=mode),
            reply_markup=stream_mode_keyboard(mode),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh stream_mode picker", exc_info=True)
    await callback.answer(t("ui.stream_mode_changed", mode=mode))


@router.message(Command("mode"))
async def handle_mode_command(message: Message, topic_config: TopicConfig) -> None:
    """Show a 2-button picker to switch exec_mode for the current topic."""
    config_id = config_id_for_channel(channel_key(message))
    if config_id is None:
        await message.answer(t("ui.exec_mode_not_in_forum"))
        return
    current = topic_config.get_topic(config_id).exec_mode
    await message.answer(
        _exec_mode_picker_caption(current),
        reply_markup=exec_mode_keyboard(current),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("exec_mode:"))
async def on_exec_mode_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
) -> None:
    """Apply a new exec_mode for the topic the picker was posted in.

    Order matters: busy-check precedes any side-effect, and tmux.kill strictly
    precedes the config write on tmux→subprocess (Decision 2 — if we wrote
    first and crashed, the next message would race a still-running tmux
    session against a fresh subprocess under the new mode).
    """
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    # InaccessibleMessage has no thread_id / edit methods — bail out if the
    # picker message is no longer reachable.
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, new_mode = callback.data.partition(":")
    # Re-validate against the whitelist even though the keyboard only emits
    # two canonical values — raw callback.data is user-controlled.
    if new_mode not in _VALID_EXEC_MODES:
        await callback.answer(t("ui.exec_mode_invalid"), show_alert=True)
        return

    key = (callback.message.chat.id, callback.message.message_thread_id)
    config_id = config_id_for_channel(key)
    if config_id is None:
        await callback.answer(t("ui.exec_mode_not_in_forum"), show_alert=True)
        return

    previous_mode = topic_config.get_topic(config_id).exec_mode

    if new_mode == previous_mode:
        await callback.answer(t("ui.exec_mode_already", mode=_exec_mode_label(new_mode)))
        return

    # Busy-check covers both channels: tmux's own processing flag AND the
    # subprocess-path MessageQueue (lock held OR items pending). Either way
    # we refuse the switch without touching tmux state.
    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    # tmux→subprocess: kill first, then persist. Reverse order leaves an
    # orphan tmux session if the write fails.
    if previous_mode == "tmux" and new_mode == "subprocess":
        await tmux_manager.kill(key)

    ok = await topic_config.update_exec_mode(config_id, new_mode)
    if not ok:
        await callback.answer(t("ui.exec_mode_write_failed"), show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else None
    logger.info(
        "exec_mode switched: user_id=%s config_id=%s previous_mode=%s new_mode=%s",
        user_id,
        config_id,
        previous_mode,
        new_mode,
    )

    # Refresh both caption and keyboard so the visible current value matches the checkmark.
    try:
        await callback.message.edit_text(
            _exec_mode_picker_caption(new_mode),
            reply_markup=exec_mode_keyboard(new_mode),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh exec_mode picker", exc_info=True)
    await callback.answer(t("ui.exec_mode_changed", mode=_exec_mode_label(new_mode)))


@router.message(Command("engine"))
async def handle_engine_command(message: Message, topic_config: TopicConfig) -> None:
    """Show provider engine picker for the current forum topic."""
    config_id = config_id_for_channel(channel_key(message))
    if config_id is None:
        await message.answer(t("ui.engine_not_in_forum"))
        return
    settings = topic_config.get_topic(config_id)
    await message.answer(
        t(
            "ui.engine_picker_caption",
            engine=engine_display_name(settings.engine),
        ),
        reply_markup=engine_keyboard(settings.engine),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("engine:"))
async def on_engine_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    session_manager: SessionManager,
) -> None:
    """Apply provider engine changes for the picker topic."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, raw_value = callback.data.partition(":")
    key = (callback.message.chat.id, callback.message.message_thread_id)
    config_id = config_id_for_channel(key)
    if config_id is None:
        await callback.answer(t("ui.engine_not_in_forum"), show_alert=True)
        return
    current = topic_config.get_topic(config_id)

    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    if raw_value not in _VALID_ENGINES:
        await callback.answer(t("ui.engine_invalid"), show_alert=True)
        return
    new_engine: Engine = "claude" if raw_value == "claude" else "codex"

    if new_engine == current.engine:
        await callback.answer(t("ui.engine_already"))
        return

    if current.models:
        ok = await topic_config.update_engine(config_id, new_engine)
    else:
        ok = await topic_config.update_engine_model(config_id, new_engine, None)
    if not ok:
        await callback.answer(t("ui.engine_write_failed"), show_alert=True)
        return

    if tmux_manager.is_active(key):
        await tmux_manager.kill(key)
    await session_manager.clear_provider_session(key)

    logger.info(
        "engine switched: user_id=%s config_id=%s previous=%s new=%s model=%s",
        callback.from_user.id if callback.from_user else None,
        config_id,
        current.engine,
        new_engine,
        current.models.get(new_engine, current.model),
    )
    engine_name = engine_display_name(new_engine)
    try:
        await callback.message.edit_text(
            t("ui.engine_picker_caption", engine=engine_name),
            reply_markup=engine_keyboard(new_engine),
            parse_mode="HTML",
        )
    except Exception:
        logger.debug("Failed to refresh engine picker", exc_info=True)
    await callback.answer(t("ui.engine_changed", engine=engine_name))
    await callback.message.answer(t("ui.engine_changed_new_session", engine=engine_name))
