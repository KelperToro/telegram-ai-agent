"""Delivery callbacks for recovering durable tmux tails after a bot restart."""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode

from telegram_bot.core.handlers.streaming import split_html_message, stream_event_action
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import SessionManager, StreamEvent
from telegram_bot.core.services.live_buffer import LiveStatusBuffer
from telegram_bot.core.services.rich_sender import send_rich_final_answer
from telegram_bot.core.services.telegram_utils import SendOutcome, send_html_with_fallback
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig, config_id_for_channel
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)


def make_recovery_on_event(
    bot: Bot,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> Callable[[ChannelKey], Callable[[StreamEvent], Awaitable[bool | None]]]:
    """Factory producing per-channel recovery on_event callbacks.

    Recovery tail drains output.jsonl after a bot restart. Status events
    are routed per the topic's current stream_mode (resolved fresh on every
    event — a /stream flip mid-tail takes effect on the next event):

      live    — lazy-create a thinking message and stream tool-call progress
                into a LiveStatusBuffer.
      verbose — send each status as its own silent HTML message; no buffer.
      minimal — drop status events entirely.

    Human-readable ``text`` events are always sent as separate messages in
    every mode. Stream mode controls only tool/status delivery.

    ``result_message`` is always delivered and the message-to-session mapping
    recorded so replies to post-restart answers trigger session switching.
    """

    def _factory(channel_key: ChannelKey) -> Callable[[StreamEvent], Awaitable[bool | None]]:
        chat_id, thread_id = channel_key
        # Cooldown, not a permanent latch: one transient failure (flood wait
        # right after restart, network blip) must not disable the live buffer
        # for the channel's whole recovery tail.
        _buffer_retry_at = 0.0
        _closed_turn_ids: set[str] = set()
        _delivered_turn_ids: set[str] = set()

        async def _ensure_buffer(turn_id: str | None) -> LiveStatusBuffer | None:
            nonlocal _buffer_retry_at
            if time.monotonic() < _buffer_retry_at:
                return None
            existing = tmux_manager.get_buffer(channel_key)
            if existing is not None:
                # Only this function calls set_buffer for a recovery channel,
                # so non-None is a LiveStatusBuffer created below. isinstance()
                # would fail in tests when the class is patched.
                return existing  # type: ignore[return-value]
            try:
                sent = await bot.send_message(
                    chat_id,
                    t("ui.thinking"),
                    message_thread_id=thread_id,
                    disable_notification=True,
                )
                buf = LiveStatusBuffer(
                    bot=bot,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    initial_message_id=sent.message_id,
                )
                await tmux_manager.set_buffer(channel_key, buf, turn_id=turn_id)
                return buf
            except Exception:
                logger.warning(
                    "Recovery tail: failed to create live buffer for %s",
                    channel_key,
                    exc_info=True,
                )
                _buffer_retry_at = time.monotonic() + 60.0
                return None

        async def _close_progress(turn_id: str | None) -> None:
            token = turn_id or ""
            if token in _closed_turn_ids:
                return
            _closed_turn_ids.add(token)
            if tmux_manager.get_buffer(channel_key) is not None:
                await tmux_manager.close_buffer(channel_key, turn_id)

        async def _send_status_verbose(content: str) -> None:
            # Status strings are plain text from the tool-status mapper.
            # Literal <, >, & in paths and tool args must not become HTML.
            safe = html.escape(content)
            await send_html_with_fallback(
                send_html=lambda: bot.send_message(
                    chat_id,
                    safe,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread_id,
                    disable_notification=True,
                ),
                send_plain=lambda: bot.send_message(
                    chat_id,
                    content,
                    message_thread_id=thread_id,
                    disable_notification=True,
                ),
                label=f"recovery_status {channel_key}",
            )

        async def _send_text_or_result(content: str) -> bool:
            """Send one normalized final and record it for reply-to-resume."""
            if not content:
                return True
            delivered_message_ids: list[int] = []
            answer_snapshot = tmux_manager.get_session_snapshot(channel_key)

            def _record_answer(message_id: int) -> None:
                if answer_snapshot is None:
                    return
                sid, provider, model = answer_snapshot
                session_manager.record_message(
                    message_id,
                    sid,
                    channel_key,
                    provider=provider,
                    model=model,
                )

            async def _send_legacy_chunk(chunk: str) -> SendOutcome:
                async def _send_recovery_html(c: str = chunk) -> Any:
                    return await bot.send_message(
                        chat_id,
                        c,
                        parse_mode=ParseMode.HTML,
                        message_thread_id=thread_id,
                    )

                async def _send_recovery_plain(c: str = chunk) -> Any:
                    return await bot.send_message(
                        chat_id,
                        c,
                        message_thread_id=thread_id,
                    )

                return await send_html_with_fallback(
                    send_html=_send_recovery_html,
                    send_plain=_send_recovery_plain,
                    label=f"recovery_tail {channel_key}",
                )

            async def _send_legacy_text(text: str) -> SendOutcome:
                last_outcome = SendOutcome(message_id=None)
                complete = True
                for chunk in split_html_message(text):
                    outcome = await _send_legacy_chunk(chunk)
                    last_outcome = outcome
                    if outcome.message_id is not None:
                        delivered_message_ids.append(outcome.message_id)
                        _record_answer(outcome.message_id)
                    if outcome.fatal or outcome.message_id is None:
                        complete = False
                        break
                return SendOutcome(
                    message_id=last_outcome.message_id,
                    fatal=last_outcome.fatal,
                    complete=complete,
                )

            def _record_rich(message_id: int) -> None:
                delivered_message_ids.append(message_id)
                _record_answer(message_id)

            try:
                outcome = await send_rich_final_answer(
                    final_text=content,
                    send_rich=lambda rich_message: bot.send_rich_message(
                        chat_id,
                        rich_message,
                        message_thread_id=thread_id,
                    ),
                    legacy_fallback=lambda: _send_legacy_text(content),
                    legacy_chunk_fallback=_send_legacy_text,
                    on_rich_sent=_record_rich,
                    label=f"recovery_tail {channel_key}",
                    flood_retry_limit=300.0,
                )
            except Exception:
                logger.warning(
                    "Recovery rich send wrapper failed for %s; using legacy sender",
                    channel_key,
                    exc_info=True,
                )
                outcome = await _send_legacy_text(content)

            # Compatibility with injected/test helpers that return a successful
            # SendOutcome but do not invoke the per-rich callback contract.
            if not delivered_message_ids and outcome.message_id is not None:
                delivered_message_ids.append(outcome.message_id)
                _record_answer(outcome.message_id)
            return outcome.complete and bool(delivered_message_ids)

        async def on_event(event: StreamEvent) -> bool | None:
            logger.debug(
                "recovery on_event channel=%s type=%s len=%d preview=%r",
                channel_key,
                event.type,
                len(event.content or ""),
                (event.content or "")[:60],
            )
            if event.type in {"status", "text", "result_message"} and not event.content.strip():
                return None
            stream_mode = topic_config.get_topic(config_id_for_channel(channel_key)).stream_mode
            action = stream_event_action(stream_mode, event)
            if action == "drop":
                return None
            if action == "turn_start":
                if stream_mode == "live":
                    await tmux_manager.activate_next_buffer(channel_key, event.turn_id)
                else:
                    await tmux_manager.discard_next_buffer(channel_key)
                return None
            if action == "turn_end":
                await _close_progress(event.turn_id)
                return None
            if action == "separate_progress":
                if event.type == "status":
                    await _send_status_verbose(event.content)
                    return None
                return await _send_text_or_result(event.content)
            if action == "buffer_progress":
                buf = await _ensure_buffer(event.turn_id)
                if buf is not None:
                    await buf.append(html.escape(event.content))
                return None
            if action == "final" and event.content:
                if event.turn_id and event.turn_id in _delivered_turn_ids:
                    logger.warning(
                        "Recovery tail: dropping duplicate final for %s turn_id=%s",
                        channel_key,
                        event.turn_id,
                    )
                    return True
                await _close_progress(event.turn_id)
                delivered = await _send_text_or_result(event.content)
                if delivered and event.turn_id:
                    _delivered_turn_ids.add(event.turn_id)
                return delivered
            return None

        return on_event

    return _factory
