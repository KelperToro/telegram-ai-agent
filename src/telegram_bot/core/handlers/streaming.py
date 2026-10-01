"""Shared streaming response helper for all handlers."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from telegram_bot.core.keyboards import topic_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager, StreamEvent
from telegram_bot.core.services.live_buffer import LiveStatusBuffer
from telegram_bot.core.services.providers import choose_available_engine, engine_display_name
from telegram_bot.core.services.rich_content import normalize_telegram_text_content
from telegram_bot.core.services.rich_sender import send_rich_final_answer
from telegram_bot.core.services.telegram_utils import (
    SendOutcome,
    send_html_with_fallback,
    send_placeholder,
)
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import (
    StreamMode,
    TopicConfig,
    config_id_for_channel,
)
from telegram_bot.core.types import ChannelKey
from telegram_bot.core.types import channel_key as get_channel_key
from telegram_bot.core.utils.telegram_html import (
    _balance_html_tags,
    _markdown_to_html_parts,
    _smart_escape,
    markdown_to_html,
    sanitize_html,
    split_html_message,
)

__all__ = [
    "_balance_html_tags",
    "_markdown_to_html_parts",
    "_smart_escape",
    "build_reply_context",
    "ensure_exec_mode_ready",
    "inject_reply_context",
    "markdown_to_html",
    "resolve_reply_target",
    "sanitize_html",
    "send_streaming_response",
    "send_to_tmux_if_active",
    "split_html_message",
    "stream_event_action",
]

# Default when topic_config is not wired in (standalone / legacy tests).
# "verbose" preserves pre-Wave-2 behavior: every status becomes its own message.
_DEFAULT_CALLER_STREAM_MODE: StreamMode = "verbose"

# Per-key lock — see ensure_exec_mode_ready docstring.
_lazy_start_locks: dict[ChannelKey, asyncio.Lock] = {}

DeliveryAction = Literal[
    "drop",
    "separate_progress",
    "buffer_progress",
    "final",
    "turn_start",
    "turn_end",
]


def _resolve_stream_mode(
    topic_config: TopicConfig | None,
    channel_key: ChannelKey,
) -> StreamMode:
    """Pick the stream_mode for a channel, falling back to verbose when unknown."""
    if topic_config is None:
        return _DEFAULT_CALLER_STREAM_MODE
    return topic_config.get_topic(config_id_for_channel(channel_key)).stream_mode


def stream_event_action(mode: StreamMode | str, event: StreamEvent) -> DeliveryAction:
    """Return the provider-neutral delivery decision for one normalized event."""
    if event.type == "turn_start":
        return "turn_start"
    if event.type == "turn_end" or event.type == "result":
        return "turn_end"
    if event.type == "result_message":
        return "final"
    if event.type == "text":
        return "separate_progress"
    if event.type != "status":
        return "drop"
    if mode == "minimal":
        return "drop"
    if mode == "live":
        return "buffer_progress"
    return "separate_progress"


_MAX_REPLY_CONTEXT_LEN = 2000


def resolve_reply_target(
    message: Message,
    session_manager: SessionManager,
) -> str | None:
    """Resolve reply-to-resume target from message.reply_to_message.

    Returns target_session_id or None if no reply, no matching session,
    or if the replied-to message belongs to a different channel (cross-topic guard).
    """
    if message.reply_to_message is None:
        return None
    current_channel = get_channel_key(message)
    return session_manager.resolve_reply_session(
        message.reply_to_message.message_id, current_channel
    )


def build_reply_context(message: Message) -> str | None:
    """Extract text from the message being replied to, preserving links.

    Returns formatted text for prompt injection, or None if no reply / no text.
    Used when user replies to a bot message that has no associated session
    (e.g. briefing notifications, task reminders).
    """
    reply = message.reply_to_message
    if reply is None:
        return None
    result = normalize_telegram_text_content(reply).text
    if not result:
        return None
    if len(result) > _MAX_REPLY_CONTEXT_LEN:
        result = result[:_MAX_REPLY_CONTEXT_LEN] + t("cc.message_truncated")
    return result


def inject_reply_context(prompt: str, reply_context: str) -> str:
    """Wrap prompt with reply context for agent to see what was replied to."""
    return t("cc.reply_context", context=reply_context, reply=prompt)


async def send_to_tmux_if_active(
    key: ChannelKey,
    prompt: str,
    source_msg: Message,
    tmux_manager: TmuxManager,
) -> bool:
    """Send prompt directly to tmux CC stdin if a tail is active.

    Returns True if dispatched to tmux (caller should return immediately),
    False if not in active tmux tail (caller should enqueue normally).

    Creates a "Thinking..." placeholder only while the provider is idle.
    A prompt accepted during an active turn is a clarification of that turn,
    so it keeps the current live buffer instead of creating an orphan candidate.
    """
    msg_id = source_msg.message_id
    if not (tmux_manager.is_active(key) and tmux_manager.is_tailing(key)):
        logger.info(
            "MSG_TRACE send_to_tmux_if_active skip channel=%s msg=%d active=%s tailing=%s",
            key,
            msg_id,
            tmux_manager.is_active(key),
            tmux_manager.is_tailing(key),
        )
        return False
    logger.info(
        "MSG_TRACE send_to_tmux_if_active dispatch channel=%s msg=%d via=send_direct",
        key,
        msg_id,
    )

    stream_mode = _resolve_stream_mode(
        tmux_manager.get_topic_config(),  # type: ignore[arg-type]
        key,
    )
    thinking_msg = None
    thinking_text = t("ui.thinking")
    if not tmux_manager.is_processing(key):
        cmd = prompt.split()[0] if prompt.startswith("/") else None
        thinking_text = t("ui.running_command", command=cmd) if cmd else t("ui.thinking")
        thinking_msg = await send_placeholder(
            lambda: source_msg.answer(thinking_text, disable_notification=True),
            label="thinking placeholder (tmux)",
        )

    queued_buffer = False
    if stream_mode == "live" and tmux_manager.live_buffer_available() and thinking_msg is not None:
        bot = tmux_manager.get_live_bot()
        new_buffer = LiveStatusBuffer(
            bot=bot,  # type: ignore[arg-type]
            chat_id=source_msg.chat.id,
            thread_id=key[1],
            initial_message_id=thinking_msg.message_id,
            header_text=thinking_text,
        )
        # The incoming Telegram message is only a candidate owner. The active
        # progress changes when the transcript confirms the next provider turn.
        await tmux_manager.queue_buffer(key, new_buffer)
        queued_buffer = True

    delivered = await tmux_manager.send_direct(key, prompt)
    if not delivered:
        # Modal-blocked or send-keys failure: the thinking placeholder is
        # a lie (CC never received the prompt). Roll back both the
        # placeholder and the LiveStatusBuffer so the user doesn't see
        # an eternal "Thinking..." with no response. send_direct has
        # already posted a modal alert with the pane snapshot; after
        # the user dismisses the modal and resends, a fresh placeholder
        # spawns for that next attempt.
        if thinking_msg is not None:
            with contextlib.suppress(TelegramAPIError):
                await thinking_msg.delete()
        if queued_buffer:
            await tmux_manager.discard_last_buffer(key)
    return True


logger = logging.getLogger(__name__)


async def ensure_exec_mode_ready(
    key: ChannelKey,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    session_manager: SessionManager,
    source_msg: Message,
) -> bool:
    """Idempotent lazy-start for tmux mode. Returns False only on RuntimeError.

    No-op paths (all return True): tmux already active, or exec_mode != "tmux".
    When exec_mode == "tmux" and tmux is dormant, starts a tmux session using
    the current channel's session blueprint (mode / cwd / mcp_config / chat_id)
    without touching queue or session state — any reset is _reset_channel's
    responsibility.

    On RuntimeError: notifies via source_msg.answer(t("ui.tmux_failed")) and
    returns False. Does NOT touch topic_config — the next user action will
    retry start_session again (no retry-suppression latch by design).
    Non-RuntimeError exceptions from start_session propagate to the caller.

    Per-key asyncio.Lock (`_lazy_start_locks`, module-level, grows monotonically
    by one entry per channel — same pattern as inbox._chat_locks) serializes
    concurrent calls on the same channel so only one start_session actually
    runs; the second caller re-checks is_active inside the critical section
    and returns True.
    """
    msg_id = source_msg.message_id
    lock = _lazy_start_locks.setdefault(key, asyncio.Lock())
    waiting = lock.locked()
    if waiting:
        logger.info(
            "MSG_TRACE ensure_exec_mode_ready waiting_on_lazy_lock channel=%s msg=%d",
            key,
            msg_id,
        )
    async with lock:
        if tmux_manager.is_active(key):
            logger.info(
                "MSG_TRACE ensure_exec_mode_ready already_active channel=%s msg=%d",
                key,
                msg_id,
            )
            return True

        settings = topic_config.get_topic(config_id_for_channel(key))
        if settings.exec_mode != "tmux":
            return True

        logger.info(
            "MSG_TRACE ensure_exec_mode_ready start_session_begin channel=%s msg=%d",
            key,
            msg_id,
        )

        current_session = session_manager._get_session(key)
        mode = current_session.mode
        cwd = current_session.cwd
        mcp_config = current_session.mcp_config
        chat_id = current_session.chat_id
        engine = current_session.engine
        model = current_session.model

        config_id = config_id_for_channel(key)
        requested_engine = (
            topic_config.get_topic(config_id).engine if config_id is not None else engine
        )
        available_engine = choose_available_engine(requested_engine)
        if available_engine is None:
            await source_msg.answer(t("ui.agent_cli_not_found"))
            return False
        if available_engine != requested_engine:
            logger.warning(
                "Engine %s unavailable for tmux channel %s; refusing provider fallback to %s",
                requested_engine,
                key,
                available_engine,
            )
            await source_msg.answer(t("ui.agent_cli_not_found"))
            return False

        # Always spawn fresh — never --resume from peek_saved_session here.
        # Lazy-start-with-resume caused a silent delivery desync in production:
        # the bot tailed a jsonl that no longer received CC's output. Root
        # cause still under investigation (tracked in the internal session-
        # rotation ticket). restore_all and switch_session still use --resume;
        # a proper fix (pid→sessionId pointer via ~/.claude/sessions/<pid>.json)
        # is tracked separately.
        try:
            started = await tmux_manager.start_session(
                key,
                mode=mode,
                cwd=cwd,
                mcp_config=mcp_config,
                chat_id=chat_id,
                session_manager=session_manager,
                provider=engine,
                model=model,
            )
        except RuntimeError as exc:
            logger.error("Lazy tmux start failed for %s: %s", key, exc)
            await source_msg.answer(t("ui.tmux_failed", exc=exc))
            return False

        logger.info(
            "MSG_TRACE ensure_exec_mode_ready start_session_done channel=%s msg=%d",
            key,
            msg_id,
        )
        if started:
            await source_msg.answer(
                t("ui.tmux_started_engine", engine=engine_display_name(engine)),
                disable_notification=True,
            )
        return True


@dataclass
class _StreamCtx:
    """Shared state for per-mode on_event handlers.

    Handlers mutate ``send_failed`` directly;
    ``sent_message_ids`` is a shared list reference used for bookkeeping.
    Ctx lifetime spans a single ``send_streaming_response`` call — not
    shared across concurrent requests, so no locking is needed.
    """

    message: Message
    channel_key: ChannelKey
    session_manager: SessionManager
    tmux_manager: TmuxManager | None
    stream_mode: StreamMode
    used_tmux: bool
    live_buffer: LiveStatusBuffer | None
    sent_message_ids: list[int]
    send_failed: bool = False


async def _send_status_silent(ctx: _StreamCtx, content: str) -> None:
    """Send a status event as its own silent message; flip ``send_failed`` on fatal."""
    # html.escape: status strings are plain text (tool names, file paths).
    safe_status = html.escape(content)
    outcome = await send_html_with_fallback(
        send_html=lambda: ctx.message.answer(
            safe_status, parse_mode=ParseMode.HTML, disable_notification=True
        ),
        send_plain=lambda: ctx.message.answer(content, disable_notification=True),
        label=f"status {ctx.channel_key}",
    )
    if outcome.message_id is not None:
        ctx.sent_message_ids.append(outcome.message_id)
    if outcome.fatal:
        ctx.send_failed = True


async def _format_and_send_chunks(
    ctx: _StreamCtx,
    content: str,
    *,
    label: str,
    record_fn: Callable[[int], None] | None = None,
) -> bool:
    """Split *content* into HTML chunks and send with plain fallback.

    Short-circuits on the first fatal outcome and flips ``ctx.send_failed``
    so downstream events also bail out. ``record_fn`` (if given) is invoked
    for each successfully-sent chunk — used by the tmux path to record
    message_id → session_id for reply-to-resume. ``split_html_message``
    already runs markdown→HTML + sanitize, so callers pass raw content.
    """
    chunks = split_html_message(content)
    for chunk in chunks:

        async def _send_html(c: str = chunk) -> Any:
            return await ctx.message.answer(c, parse_mode=ParseMode.HTML)

        async def _send_plain(c: str = chunk) -> Any:
            return await ctx.message.answer(c)

        outcome = await send_html_with_fallback(
            send_html=_send_html,
            send_plain=_send_plain,
            label=label,
        )
        if outcome.message_id is not None:
            ctx.sent_message_ids.append(outcome.message_id)
            if record_fn is not None:
                record_fn(outcome.message_id)
        if outcome.fatal:
            ctx.send_failed = True
            return False
        if outcome.message_id is None:
            return False
    return True


def _tmux_session_snapshot(ctx: _StreamCtx) -> tuple[str, str | None, str | None] | None:
    """Capture the answer event's authoritative tmux session identity."""
    if not ctx.used_tmux or ctx.tmux_manager is None:
        return None
    return ctx.tmux_manager.get_session_snapshot(ctx.channel_key)


def _record_tmux_message(
    ctx: _StreamCtx,
    msg_id: int,
    snapshot: tuple[str, str | None, str | None] | None,
) -> None:
    """Bind *msg_id* to an answer event's immutable tmux session snapshot.

    Must happen inside ``on_event`` because the tmux tail is long-lived
    (exits only on /cancel, /clear, tmux death, or 6h timeout), so any
    post-stream recording would fire hours after the user's message — if ever.
    The snapshot is captured before Telegram I/O so a reply-driven session
    switch during flood wait cannot rebind an old answer to the new session.
    """
    if snapshot is None:
        return
    sid, provider, model = snapshot
    ctx.session_manager.record_message(msg_id, sid, ctx.channel_key, provider=provider, model=model)


async def _send_tmux_answer(ctx: _StreamCtx, content: str, *, label: str) -> bool:
    """Deliver one tmux answer event through the rich/fallback final-answer contract.

    Tmux tails emit answers while ``send_stream`` is still running, so delivery and
    reply-to-resume recording must both happen inside ``on_event``. Rich success
    records the returned message directly; the legacy callback keeps the existing
    per-chunk recording behavior and prevents duplicate bookkeeping after fallback.
    """
    snapshot = _tmux_session_snapshot(ctx)
    legacy_message_ids: list[int] = []

    async def _send_legacy_text(text: str) -> SendOutcome:
        before = len(ctx.sent_message_ids)
        complete = await _format_and_send_chunks(
            ctx,
            text,
            label=label,
            record_fn=lambda mid: _record_tmux_message(ctx, mid, snapshot),
        )
        legacy_message_ids.extend(ctx.sent_message_ids[before:])
        last_id = legacy_message_ids[-1] if legacy_message_ids else None
        return SendOutcome(
            message_id=last_id,
            fatal=ctx.send_failed,
            complete=complete,
        )

    async def _send_legacy() -> SendOutcome:
        return await _send_legacy_text(content)

    def _record_rich(message_id: int) -> None:
        ctx.sent_message_ids.append(message_id)
        _record_tmux_message(ctx, message_id, snapshot)

    outcome = await send_rich_final_answer(
        final_text=content,
        send_rich=lambda rich_message: ctx.message.answer_rich(rich_message),
        legacy_fallback=_send_legacy,
        legacy_chunk_fallback=_send_legacy_text,
        on_rich_sent=_record_rich,
        label=f"{label}_rich",
        flood_retry_limit=300.0,
    )
    if outcome.fatal:
        ctx.send_failed = True
    return outcome.complete and (outcome.message_id is not None or bool(legacy_message_ids))


async def _handle_text_event(ctx: _StreamCtx, event: StreamEvent) -> None:
    """Send verbose intermediate text as its own Telegram message."""
    if not ctx.used_tmux:
        await _format_and_send_chunks(
            ctx,
            event.content,
            label=f"text {ctx.channel_key}",
        )
        return

    await _send_tmux_answer(
        ctx,
        event.content,
        label=f"text {ctx.channel_key}",
    )


async def _handle_result_message_event(ctx: _StreamCtx, event: StreamEvent) -> bool:
    """Deliver one normalized final answer, including tmux turns."""
    label = f"result_message {ctx.channel_key}"
    if ctx.used_tmux:
        return await _send_tmux_answer(ctx, event.content, label=label)
    before = len(ctx.sent_message_ids)
    complete = await _format_and_send_chunks(ctx, event.content, label=label)
    return complete and len(ctx.sent_message_ids) > before


async def _send_final_response(ctx: _StreamCtx, final_text: str) -> None:
    """Send the concluding response with the topic keyboard (groups only).

    Records response message_ids under the current session for
    reply-to-resume. Status/text/result messages sent during streaming are
    NOT recorded here — users reply to the final answer, not to intermediate
    progress updates. Aborts early if a prior handler already flipped
    ``send_failed`` (Telegram clearly rejecting everything).
    """
    # Supergroup forum: topic_keyboard (3 buttons in a row)
    # Private chat: no keyboard (buttons would go to General topic)
    is_group = ctx.message.chat.type == ChatType.SUPERGROUP
    reply_kb = topic_keyboard() if is_group else None

    response_message_ids: list[int] = []

    async def _send_legacy_text(text: str) -> SendOutcome:
        return await _send_legacy_final_response(ctx, text, reply_kb, response_message_ids)

    async def _send_legacy_final() -> SendOutcome:
        return await _send_legacy_text(final_text)

    if not ctx.send_failed:
        outcome = await send_rich_final_answer(
            final_text=final_text,
            send_rich=lambda rich_message: ctx.message.answer_rich(
                rich_message,
                reply_markup=reply_kb,
            ),
            legacy_fallback=_send_legacy_final,
            legacy_chunk_fallback=_send_legacy_text,
            on_rich_sent=response_message_ids.append,
            label=f"final_rich {ctx.channel_key}",
            flood_retry_limit=300.0,
        )
        if outcome.fatal:
            ctx.send_failed = True

    # Record only final response message IDs for reply-to-resume
    # (users reply to responses, not intermediate status messages).
    current_sid = ctx.session_manager.get_current_session_id(ctx.channel_key)
    if current_sid:
        for msg_id in response_message_ids:
            ctx.session_manager.record_message(msg_id, current_sid, ctx.channel_key)


async def _send_legacy_final_response(
    ctx: _StreamCtx,
    final_text: str,
    reply_kb: Any,
    response_message_ids: list[int],
) -> SendOutcome:
    """Send final answer through the established HTML/plain fallback path."""
    chunks = split_html_message(final_text)
    for chunk in chunks:
        if ctx.send_failed:
            break

        async def _send_html(c: str = chunk) -> Any:
            return await ctx.message.answer(c, parse_mode=ParseMode.HTML, reply_markup=reply_kb)

        async def _send_plain(c: str = chunk) -> Any:
            return await ctx.message.answer(c, reply_markup=reply_kb)

        outcome = await send_html_with_fallback(
            send_html=_send_html,
            send_plain=_send_plain,
            label=f"final_chunk {ctx.channel_key}",
            flood_retry_limit=300.0,
        )
        if outcome.message_id is not None:
            response_message_ids.append(outcome.message_id)
        if outcome.fatal:
            ctx.send_failed = True
            return SendOutcome(
                message_id=outcome.message_id,
                fatal=outcome.fatal,
                complete=False,
            )
        if outcome.message_id is None:
            return SendOutcome(
                message_id=response_message_ids[-1] if response_message_ids else None,
                complete=False,
            )
    last_id = response_message_ids[-1] if response_message_ids else None
    return SendOutcome(
        message_id=last_id,
        fatal=ctx.send_failed,
        complete=not ctx.send_failed and last_id is not None,
    )


async def send_streaming_response(
    message: Message,
    session_manager: SessionManager,
    channel_key: ChannelKey,
    prompt: str,
    git_sync: Any | None = None,
    tmux_manager: TmuxManager | None = None,
    topic_config: TopicConfig | None = None,
    image_paths: tuple[str, ...] = (),
) -> None:
    """Send prompt to CC with streaming and deliver response to user.

    stream_mode controls what reaches Telegram between the thinking placeholder
    and the final result:
      verbose — every status event ships as its own silent message (legacy).
      minimal — status events dropped; only the thinking + results stay,
                which is what project topics want so agent-team chatter
                doesn't hit the SendMessage flood limit.
      live    — status events are appended to a single editable
                ``LiveStatusBuffer`` message; falls back to verbose behaviour
                for status when no buffer is available.
    All message IDs are still recorded for reply-to-resume.
    """
    stream_mode = _resolve_stream_mode(topic_config, channel_key)
    previous_session_id = session_manager.get_current_session_id(channel_key)
    # User-content preview — DEBUG only to keep INFO journalctl clean of PII.
    logger.debug(
        "Prompt to CC (channel %s, stream_mode=%s): %.200s",
        channel_key,
        stream_mode,
        prompt,
    )

    sent_message_ids: list[int] = []

    tmux_required = False
    if topic_config is not None:
        try:
            tmux_required = (
                topic_config.get_topic(config_id_for_channel(channel_key)).exec_mode == "tmux"
            )
        except Exception:
            logger.warning("Failed to resolve exec_mode for %s", channel_key, exc_info=True)
            await message.answer(t("ui.tmux_failed", exc="topic config is unreadable"))
            return

    cmd = prompt.split()[0] if prompt.startswith("/") else None
    thinking_text = t("ui.running_command", command=cmd) if cmd else t("ui.thinking")
    used_tmux = tmux_manager is not None and tmux_manager.is_active(channel_key)
    if tmux_required and not used_tmux:
        logger.warning(
            "Refusing subprocess fallback for tmux-configured channel %s",
            channel_key,
        )
        await message.answer(t("ui.tmux_failed", exc="tmux session is not active"))
        return

    thinking_msg = await send_placeholder(
        lambda: message.answer(thinking_text, disable_notification=True),
        label="thinking placeholder (subprocess)",
    )
    if thinking_msg is not None:
        sent_message_ids.append(thinking_msg.message_id)

    # Materialize a LiveStatusBuffer for live-mode. For tmux it's registered
    # on the manager so on_event (which may fire from a long-running tail)
    # can always look up the current buffer. For non-tmux the buffer lives
    # in this function's closure and is closed in finally.
    live_buffer: LiveStatusBuffer | None = None
    if (
        stream_mode == "live"
        and message.bot is not None
        and not used_tmux  # tmux case wires a fresh buffer below
        and thinking_msg is not None  # no placeholder → nothing to edit, fall back to verbose
    ):
        live_buffer = LiveStatusBuffer(
            bot=message.bot,
            chat_id=message.chat.id,
            thread_id=channel_key[1],
            initial_message_id=thinking_msg.message_id,
            header_text=thinking_text,
        )
    if (
        stream_mode == "live"
        and used_tmux
        and tmux_manager is not None
        and tmux_manager.live_buffer_available()
        and message.bot is not None
        and thinking_msg is not None
    ):
        bot = tmux_manager.get_live_bot()
        tmux_buffer = LiveStatusBuffer(
            bot=bot,  # type: ignore[arg-type]
            chat_id=message.chat.id,
            thread_id=channel_key[1],
            initial_message_id=thinking_msg.message_id,
            header_text=thinking_text,
        )
        await tmux_manager.queue_buffer(channel_key, tmux_buffer)

    ctx = _StreamCtx(
        message=message,
        channel_key=channel_key,
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        stream_mode=stream_mode,
        used_tmux=used_tmux,
        live_buffer=live_buffer,
        sent_message_ids=sent_message_ids,
    )

    async def _ensure_tmux_live_buffer(turn_id: str | None) -> LiveStatusBuffer | None:
        if not ctx.used_tmux or ctx.tmux_manager is None:
            return ctx.live_buffer
        raw = ctx.tmux_manager.get_buffer(ctx.channel_key)
        if raw is not None and callable(getattr(raw, "append", None)):
            return raw  # type: ignore[return-value]
        if not ctx.tmux_manager.live_buffer_available():
            return None
        try:
            sent = await ctx.message.answer(t("ui.thinking"), disable_notification=True)
            bot = ctx.tmux_manager.get_live_bot()
            buffer = LiveStatusBuffer(
                bot=bot,  # type: ignore[arg-type]
                chat_id=ctx.message.chat.id,
                thread_id=ctx.channel_key[1],
                initial_message_id=sent.message_id,
                header_text=t("ui.thinking"),
            )
            await ctx.tmux_manager.set_buffer(
                ctx.channel_key,
                buffer,
                turn_id=turn_id,
            )
            return buffer
        except Exception:
            logger.warning(
                "Failed to create live progress buffer for %s",
                ctx.channel_key,
                exc_info=True,
            )
            return None

    async def _live_append_progress(event: StreamEvent) -> None:
        """Append progress to one editable surface; never fan out on failure."""
        buf: LiveStatusBuffer | None
        if ctx.used_tmux and ctx.tmux_manager is not None:
            raw = ctx.tmux_manager.get_buffer(ctx.channel_key)
            buf = raw if raw is not None and callable(getattr(raw, "append", None)) else None  # type: ignore[assignment]
        else:
            buf = ctx.live_buffer
        if buf is None:
            buf = await _ensure_tmux_live_buffer(event.turn_id)
        if buf is not None:
            await buf.append(html.escape(event.content))

    async def _close_turn_progress(turn_id: str | None) -> None:
        if ctx.used_tmux and ctx.tmux_manager is not None:
            if ctx.tmux_manager.get_buffer(ctx.channel_key) is not None:
                await ctx.tmux_manager.close_buffer(ctx.channel_key, turn_id)
        elif ctx.live_buffer is not None:
            await ctx.live_buffer.close()

    async def on_event(event: StreamEvent) -> bool | None:
        # send_failed latches across subsequent events: stop sending to
        # Telegram, but keep accumulating non-tmux text so the final
        # summary still assembles if the retry policy eventually recovers.
        if ctx.send_failed and event.type != "result_message":
            return None

        # Long-lived tmux tails must observe /stream changes on the next event.
        ctx.stream_mode = _resolve_stream_mode(topic_config, ctx.channel_key)

        # Central empty-content guard (W1.2). CC emits empty events at
        # compact boundaries, token-count-only events, and empty thinking
        # blocks. Forwarding those to Telegram fails — split_html_message
        # on "" yields [""], then message.answer("") → TelegramBadRequest.
        # Per-mode handlers receive only non-empty events.
        if event.type in ("status", "text", "result_message") and not event.content.strip():
            logger.debug("Dropping empty %s event on channel %s", event.type, ctx.channel_key)
            return None

        action = stream_event_action(ctx.stream_mode, event)
        if action == "drop":
            return None
        if action == "turn_start":
            if ctx.used_tmux and ctx.tmux_manager is not None:
                if ctx.stream_mode == "live":
                    await ctx.tmux_manager.activate_next_buffer(
                        ctx.channel_key,
                        event.turn_id,
                    )
                else:
                    await ctx.tmux_manager.discard_next_buffer(ctx.channel_key)
            return None
        if action == "turn_end":
            await _close_turn_progress(event.turn_id)
            return None
        if action == "buffer_progress":
            await _live_append_progress(event)
            return None
        if action == "separate_progress":
            if event.type == "status":
                await _send_status_silent(ctx, event.content)
            elif event.type == "text":
                await _handle_text_event(ctx, event)
            return None
        if action == "final":
            await _close_turn_progress(event.turn_id)
            # A prior progress-send failure must not suppress the final.
            ctx.send_failed = False
            return await _handle_result_message_event(ctx, event)
        return None

    async def _notify_engine_changed(new_engine: str) -> None:
        """Surface auto-fallback the same way manual /engine does — same wording.

        Fires before the agent's response so the user sees the engine change
        rather than only inferring it from lost context. Catch broadly: a
        notification failure must never block the agent from running.
        """
        try:
            await message.answer(
                t("ui.engine_changed_new_session", engine=engine_display_name(new_engine))
            )
        except Exception:
            logger.exception(
                "Failed to deliver engine-changed notice on channel %s",
                channel_key,
            )

    try:
        if used_tmux:
            assert tmux_manager is not None
            response = await tmux_manager.send_stream(channel_key, prompt, on_event)
            # Sync session_id back so reply-to-resume works
            new_sid = tmux_manager.get_session_id(channel_key)
            if new_sid:
                await session_manager.override_session(channel_key, new_sid)
        else:
            response = await session_manager.send_stream(
                channel_key,
                prompt,
                on_event,
                on_engine_changed=_notify_engine_changed,
                image_paths=image_paths,
            )
    except asyncio.CancelledError:
        # Status messages ARE the history — no cleanup needed
        raise
    finally:
        # Close the non-tmux live buffer if we owned one. In tmux mode the
        # buffer is owned by tmux_manager and stays alive across the tail —
        # it's closed when the next user message arrives or on /clear.
        if live_buffer is not None:
            with contextlib.suppress(Exception):
                await live_buffer.close()
            # Absorb per-page message IDs so reply-to-resume covers every page.
            for mid in live_buffer.message_ids:
                if mid not in sent_message_ids:
                    sent_message_ids.append(mid)

    # Notify git sync for knowledge mode (fire-and-forget)
    if git_sync is not None:
        mode = session_manager.get_mode(channel_key)
        if mode == "knowledge":
            try:
                git_sync.notify()
            except RuntimeError:
                logger.debug("Git sync notify skipped, event loop closing")

    # In tmux mode, results are sent immediately via result_message events.
    # In tmux each result_message is recorded inside on_event (long-lived tail),
    # so no post-stream recording is needed here.
    final_text = response
    if not final_text:
        return

    current_session_id = session_manager.get_current_session_id(channel_key)
    session = session_manager._get_session(channel_key)
    await _send_final_response(ctx, final_text)
    if (
        os.name == "nt"
        and not used_tmux
        and previous_session_id is None
        and current_session_id is not None
        and session.engine == "codex"
        and final_text != t("ui.error_generic")
    ):
        title = " ".join(prompt.partition("\n")[0].split())[:70].strip() or "Codex"
        try:
            notice = await message.answer(
                t(
                    "ui.codex_chat_created",
                    title=html.escape(f"Telegram: {title}"),
                    sid=current_session_id[:8],
                    project=html.escape(Path(session.cwd).name),
                ),
                parse_mode=ParseMode.HTML,
            )
            session_manager.record_message(notice.message_id, current_session_id, channel_key)
            logger.info(
                "Announced new Codex Desktop chat channel=%s session_id=%s",
                channel_key,
                current_session_id,
            )
        except TelegramAPIError:
            logger.warning("Could not deliver Codex chat identity", exc_info=True)
