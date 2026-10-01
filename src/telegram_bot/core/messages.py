"""Bot message localization — translate strings by key based on settings.bot_lang.

Two languages: "en" (default) and "ru". Keys group into:
- ui.*    — strings shown to the user (status, errors, button text)
- cc.*    — content prefixes injected into prompts for Claude Code
            (forwarded media labels, voice transcription markers, etc.)
- tool.*  — tool-status messages shown while CC executes a tool

Use `t("ui.thinking")` for static strings or `t("ui.tmux_failed", exc=err)`
for templates with placeholders. Missing keys fall back to English, then
to the key itself if not found in either language.

When the message file changes between languages on bot restart, the
lru_cache around _get_lang() keeps a single value for the process lifetime.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_LANG = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # --- UI: user-facing status / error messages -------------------
        "ui.start_welcome": (
            "Hi! I'll forward your messages to Claude Code. Just send me anything."
        ),
        "ui.thinking": "⏳ Thinking...",
        "ui.recognizing_voice": "⏳ Transcribing voice...",
        "ui.processing_forwards": "⏳ Processing forwarded messages...",
        "ui.processing_files": "⏳ Processing files...",
        "ui.new_session": (
            "The next message will create a new chat. Find it in Codex Desktop Recents or /resume."
        ),
        "ui.codex_chat_created": (
            "🆕 Codex Desktop chat: <b>{title}</b> in <code>{project}</code> "
            "(ID <code>{sid}</code>). Find it in Desktop Recents or first in /resume."
        ),
        "ui.context_cleared": "🧹 Context cleared, tmux session alive",
        "ui.reset_failed": "❌ Couldn't reset the context. Try again in a moment.",
        "ui.topic_welcome": (
            "👋 Topic ready. Tell me what it's for — which project to run in "
            "(path or name), and whether you want a custom prompt for this topic."
        ),
        "ui.cancelled": "❌ Cancelled",
        "ui.nothing_to_cancel": "Nothing to cancel",
        "ui.language_current": "Language: <b>{lang}</b>. Use /language ru or /language en.",
        "ui.language_changed": "Language switched to <b>{lang}</b>.",
        "ui.language_invalid": "Unknown language. Use /language ru or /language en.",
        "ui.already_finished": "Process already finished",
        "ui.tmux_failed": "❌ Failed to start tmux: {exc}",
        "ui.tmux_killed": "🗑 Tmux session killed",
        "ui.subprocess_killed": "🗑 Session stopped. The next message starts a new chat.",
        "ui.subprocess_recycled": "🔄 Current task stopped. The next message resumes this session.",
        "ui.tmux_not_active": "No active tmux session in this topic",
        "ui.recycle_done": "♻️ Runtime restarted",
        "ui.recycle_failed": "❌ Couldn't restart the runtime. Try again in a moment.",
        "ui.engine_starting": (
            "🔄 {engine} is starting up — please wait a few seconds before sending."
        ),
        "ui.engine_ready": "✅ {engine} is ready.",
        "ui.engine_start_failed": "⚠️ {engine} failed to start: {exc}",
        "ui.engine_started_input_blocked": (
            "⚠️ {engine} started but input is blocked — most likely a modal dialog."
            " Use /tui to see the pane and dismiss it."
        ),
        "ui.codex_update_status": (
            "Codex update status: <b>{status}</b>\n"
            "Last success: <code>{last_success}</code>\n"
            "{output}"
        ),
        "ui.codex_update_running": "🔄 Updating Codex...",
        "ui.codex_update_success": "✅ Codex update finished.\n<pre>{output}</pre>",
        "ui.codex_update_failed": "❌ Codex update failed ({status}).\n<pre>{output}</pre>",
        "ui.codex_update_already_running": "Codex update is already running.",
        "ui.codex_update_active_sessions": (
            "⚠️ Codex update skipped: stop active Codex sessions first with /kill."
        ),
        "ui.codex_update_cooldown": "Codex auto-update skipped: cooldown is active.",
        "ui.goal_no_session": "No active Codex chat. Send a message or choose one with /resume.",
        "ui.goal_codex_only": "Goals are available for Codex chats only.",
        "ui.goal_help": (
            "Use /goal, /goal set <objective>, /goal pause, /goal resume, "
            "/goal complete, /goal budget <tokens>, or /goal clear."
        ),
        "ui.goal_status": "<b>Goal</b>: {objective}\nStatus: {status}\nTokens: {used}/{budget}",
        "ui.goal_cleared": "Goal cleared.",
        "ui.goal_missing": "This chat has no goal. Use /goal set <objective>.",
        "ui.goal_failed": "Could not update the goal. Try again.",
        "ui.resume_no_matches": "No saved chats match that search. Try /resume with another word.",
        "ui.codex_thread_busy": (
            "Another Codex process is using this chat. Stop that run, then resend your message. "
            "Your selected chat is preserved."
        ),
        # --- UI: tail / tui feature strings (Wave 3 tmux-tui-mode) ----
        "ui.tail_unavailable": (
            "⚠️ No active tmux session — /tui is unavailable."
            " Switch to tmux mode or start the session first."
        ),
        "ui.tail_snapshot_header": "TUI snapshot (last lines):",
        "ui.tail_keyboard_stale": ("Keyboard is outdated — session restarted. Call /tui again."),
        "ui.modal_blocked_header": (
            "⚠️ Message NOT sent — CC is waiting on a modal dialog.\n"
            "Your message: <code>{prompt}</code>\n"
            "Dismiss the modal (Esc / pick an option), then resend."
        ),
        "ui.delivery_unconfirmed_header": (
            "⚠️ Delivery could not be confirmed — the message may already be "
            "waiting in Codex input.\n"
            "Your message: <code>{prompt}</code>\n"
            "Open /tui and check the input. Do not resend yet."
        ),
        "ui.modal_idle_detected": (
            "⚠️ CC is waiting on a modal dialog.\n"
            "Dismiss it (Esc / pick an option) so CC can continue."
        ),
        "ui.tui_start_timeout": (
            "❌ Claude Code TUI didn't become ready in 30 s — tmux killed."
            " Try again or /mode → regular."
        ),
        "ui.tui_session_missing": (
            "Session unavailable (created before migration) — keep writing to the current session."
        ),
        # --- UI: exec-mode picker -------------------------------------
        "ui.exec_mode_picker_caption": (
            "Execution mode: <b>{current}</b>\n\n"
            "⚡ regular — each message spawns a fresh Claude subprocess with"
            " no context from previous messages. Task runs, session dies, no"
            " resource footprint. Good for one-off tasks, notes, etc.\n\n"
            "🖥 tmux — persistent tmux session that keeps running and"
            " remembers context across messages. Full Claude Code with agent"
            " team support, but constantly consumes resources. Better for"
            " development and big tasks. First start takes ~1-2s. To kill the"
            " session and free resources — use /kill."
        ),
        "ui.exec_mode_changed": "Mode: {mode}",
        "ui.exec_mode_already": "Already: {mode}",
        "ui.exec_mode_busy": "Processing in progress, hit /cancel first",
        "ui.exec_mode_invalid": "Invalid mode",
        "ui.exec_mode_not_in_forum": "⚠️ /mode works only in forum topics",
        "ui.exec_mode_write_failed": "Failed to save mode, try again",
        "ui.exec_mode_label_subprocess": "⚡ regular",
        "ui.exec_mode_label_tmux": "🖥 tmux",
        "ui.engine_picker_caption": (
            "Engine: <b>{engine}</b>\n\n"
            "This does not change /mode: regular and tmux remain transport settings."
        ),
        "ui.engine_changed": "Engine: {engine}",
        "ui.engine_changed_new_session": (
            "Engine: {engine}.\n\nActive session was reset. The next message"
            " will start a new session with {engine}."
        ),
        "ui.engine_already": "Already selected",
        "ui.engine_invalid": "Invalid engine",
        "ui.engine_not_in_forum": "⚠️ /engine works only in forum topics",
        "ui.engine_write_failed": "Failed to save engine settings",
        "ui.tmux_started_engine": "🖥 New tmux session started with {engine}",
        "ui.reply_engine_switched": (
            "↪️ Reply target belongs to {engine}; switching this topic before resume."
        ),
        "ui.stream_mode_picker_caption": (
            "Stream mode for this topic: <b>{current}</b>\n\n"
            "• <b>verbose</b> — each event as its own message\n"
            "• <b>live</b> — one editable buffer with progress updates\n"
            "• <b>minimal</b> — only final answers"
        ),
        "ui.stream_mode_changed": "Mode: {mode}",
        "ui.stream_mode_invalid": "Unknown mode",
        "ui.stream_mode_not_in_forum": "⚠️ /stream works only in forum topics.",
        "ui.stream_mode_write_failed": "Failed to save config",
        "ui.claude_no_text": "Claude finished the turn without a text response.",
        "ui.claude_stop_sequence_no_text": (
            "Claude finished on a stop sequence without a text response."
        ),
        "ui.claude_limit_no_text": "Claude reached the output limit without a text response.",
        "ui.claude_context_no_text": ("Claude reached the context limit without a text response."),
        "ui.claude_refused_no_text": "Claude refused to answer.",
        "ui.claude_unknown_completion": (
            "Claude ended the previous turn with an unknown completion format."
        ),
        "ui.claude_limit_note": "\n\n⚠️ The answer was cut off by the output limit.",
        "ui.claude_context_note": "\n\n⚠️ The answer was cut off by the context limit.",
        "ui.session_switched": "🔄 session: {sid}",
        "ui.session_switched_engine": "🔄 {engine} session: {sid}",
        "ui.resume_picker_caption_hdr": "Sessions for <code>{cwd}</code>, page {page}/{total}",
        "ui.resume_picker_caption_all_hdr": (
            "Saved chats from all projects, page {page}/{total}. "
            "If listed, the current chat is first and marked ✅. "
            "Search by name or project: /resume &lt;word&gt;"
        ),
        "ui.resume_no_sessions": "No saved sessions for this cwd",
        "ui.resume_not_in_forum": "⚠️ /resume works only in forum topics",
        "ui.resume_subprocess_unsupported": "This command works only in tmux mode (/mode)",
        "ui.resume_already_on_it": "Already on this session",
        "ui.resume_target_missing": "Transcript disappeared; open /resume again",
        "ui.resume_invalid_id": "Invalid session ID",
        "ui.resume_starting": "Resuming...",
        "ui.resume_current_marker": "current",
        "ui.resume_switched": "🔄 session: <code>{sid}</code>",
        "ui.resume_selection_details": (
            "Chat: <b>{title}</b>\nProject: <code>{cwd}</code>\nSend a message to continue."
        ),
        "ui.resume_started": "🆕 tmux started with resume <code>{sid}</code>",
        "ui.resume_engine_switched": "↪️ Switching engine to <code>{engine}</code>",
        "ui.resume_picker_stale": "List is stale, open /resume again",
        "ui.resume_spawn_failed": "Failed to start tmux. The next message will start fresh.",
        "ui.resume_spawn_failed_engine_changed": (
            "Failed to start tmux. Engine was switched to {engine};"
            " the next message will start fresh."
        ),
        "ui.resume_config_write_failed": "Failed to update engine; nothing changed",
        "ui.resume_cancelled": "Cancelled",
        "ui.error_generic": "An error occurred while processing the request. Try again.",
        "ui.cc_not_found": "Claude Code not found. Make sure it is installed and on PATH.",
        "ui.agent_cli_not_found": (
            "I couldn't find Claude Code or Codex. Install at least one of them for the "
            "same Linux user that runs this bot, make sure it is available on PATH, "
            "then restart the bot."
        ),
        "ui.compacting": "⏳ Compacting context...",
        "ui.compact_done": "✅ Compacted: {pre:,} → {post:,} tokens",
        "ui.running_command": "⚙️ Running {command}...",
        "ui.inactivity_kill": "Hung up, try again",
        "ui.voice_too_large": "Voice message is too large (max 100 MB).",
        "ui.voice_not_recognized": "Could not transcribe voice message",
        "ui.download_error": "Couldn't download the file, try again",
        "ui.file_too_large": "File is too large (max {size} MB)",
        "ui.forward_error": "Failed to process messages, try again",
        # --- UI: keyboard buttons -------------------------------------
        "ui.btn_new_chat": "New chat",
        "ui.btn_cancel": "Cancel ❌",
        "ui.btn_tui": "TUI 🖥",
        # --- CC content prefixes (injected into prompts) --------------
        "cc.voice_label": "Voice",
        "cc.videomessage_label": "Video message",
        "cc.voice_transcript_short": "[Voice, transcription]:",
        "cc.voice_failed_full": "[Voice message: failed to transcribe]",
        "cc.voice_too_large": "[Voice message: file too large]",
        "cc.voice_empty": "[Voice message: empty transcription]",
        "cc.transcription_failed": "[{label}, not transcribed]",
        "cc.photo": "[Photo]",
        "cc.photo_with_caption": "[Photo with caption]: {caption}",
        "cc.photo_failed": "[Photo: failed to download]",
        "cc.video": "[Video]",
        "cc.videomessage": "[Video message]",
        "cc.document": "[Document]",
        "cc.document_named": "[Document: {name}]",
        "cc.document_full": "[Document: {name}, {mime}]",
        "cc.document_failed": "[Document: {name}, failed to download]",
        "cc.sticker": "[Sticker: {emoji}]",
        "cc.audio": "[Audio: {title}]",
        "cc.audio_untitled": "untitled",
        "cc.empty_message": "[Empty message]",
        "cc.unknown_sender": "Unknown sender",
        "cc.unknown_channel": "Unknown channel",
        "cc.unknown_chat": "Unknown chat",
        "cc.file_default": "file",
        # Wrap path in `[...]` so the Claude Code TUI bracketed-paste
        # splitter (split on `' /'` and `\n`, then match each token
        # against `/\.(png|jpe?g|gif|webp)$/i`) does NOT pick the path
        # up as an auto-attach image candidate. Without the brackets,
        # the async attach races the bot's Enter key and the model
        # sees `File:` empty (regression after the bracketed-paste
        # delivery switch on 2026-04-26). With `[...]` the token ends
        # in `]`, regex misses, path stays as text, agent reads it via
        # Read tool — the pre-2026-04-26 working behaviour.
        "cc.file_path": "File: [{path}]",
        "cc.file_caption": "[Photo with caption]: {caption}\nFile: [{path}]",
        "cc.batch_during_processing": ("[Messages received during processing ({count} total)]:"),
        "cc.reply_context": ("[Message the user replied to]:\n{context}\n\n[User reply]:\n{reply}"),
        "cc.message_truncated": "\n[...message truncated]",
        "cc.forward_batch": "[Forwarded messages, {count} total]:",
        "cc.forward_message_header": "--- Message {index} ---",
        "cc.forward_from": "From: {name}",
        "cc.forward_post_link": "Post link: {link}",
        "cc.forward_date": "Date: {date}",
        # See `cc.file_path` comment — same `[...]` wrap defeats the
        # Claude TUI image-attach splitter for `.png/.jpg/.gif/.webp`
        # forwarded as documents.
        "cc.attached_file": "Attached file (read via Read): [{path}]",
        "cc.caption": "Caption: {caption}",
        "cc.user_comment": "User comment: {comment}",
        "cc.photo_error": "[Photo: {error}]",
        "cc.document_error": "[Document: {name}, {mime}: {error}]",
        "cc.files_batch": "[Files, {count} total]:",
        "cc.unknown_file_type": "[Unknown file type]",
        "cc.error_generic_label": "error",
        # --- Queue messages -------------------------------------------
        "ui.queue_added_batch": "Added to batch, #{position} in queue",
        "ui.queue_added": "Added to queue (#{position})",
        "ui.queue_session_suffix": ", session: {sid}",
        "ui.queue_item_dropped": (
            "⚠️ Message processing failed — it was not delivered to the agent. Please resend."
        ),
        "ui.queue_dropped_shutdown": (
            "⚠️ Bot is restarting — your queued message was not processed. "
            "Please resend it after the restart."
        ),
        # --- Tool status (shown while CC runs a tool) -----------------
        "tool.read": "📖 Reading file",
        "tool.grep": "🔍 Searching",
        "tool.glob": "🔍 Finding files",
        "tool.bash": "⚙️ Running",
        "tool.bash_with_cmd": "⚙️ Running: {cmd}",
        "tool.write": "✏️ Writing file",
        "tool.edit": "✏️ Editing",
        "tool.skill": "📋 Loading skill",
        "tool.agent": "🤖 Launching subagent",
        "tool.agent_done": "✅ Subagent finished",
        "tool.agent_done_with_desc": "✅ Subagent finished: {desc}",
        "tool.send_message": "💬 Sending message...",
        "tool.send_image": "🖼 Sending image...",
        "tool.send_image_gallery": "🖼 Sending gallery...",
        "tool.send_document": "📎 Sending document...",
        "tool.fetch_url": "🌐 Fetching URL",
        "tool.run_tests": "🧪 Running tests",
        "tool.calc_time": "🧮 Calculating time",
        "tool.check_time": "🕐 Checking time",
        "tool.read_memory": "🧠 Reading memory",
        "tool.write_memory": "🧠 Updating memory",
        "tool.read_skill": "📋 Reading skill",
        "tool.write_skill": "📋 Updating skill",
    },
    "ru": {
        "ui.goal_no_session": (
            "Нет активного чата Codex. Отправьте сообщение или выберите чат через /resume."
        ),
        "ui.goal_codex_only": "Цели доступны только для чатов Codex.",
        "ui.goal_help": (
            "Команды: /goal, /goal set <цель>, /goal pause, /goal resume, "
            "/goal complete, /goal budget <токены>, /goal clear."
        ),
        "ui.goal_status": "<b>Цель</b>: {objective}\nСтатус: {status}\nТокены: {used}/{budget}",
        "ui.goal_cleared": "Цель удалена.",
        "ui.goal_missing": "У этого чата нет цели. Задайте её через /goal set <цель>.",
        "ui.goal_failed": "Не удалось изменить цель. Попробуйте ещё раз.",
        "ui.resume_no_matches": "По запросу чаты не найдены. Попробуйте /resume с другим словом.",
        "ui.codex_thread_busy": (
            "Этот чат занят другим процессом Codex. "
            "Остановите тот запуск и отправьте сообщение снова. "
            "Выбранный чат сохранён."
        ),
        # --- UI: user-facing status / error messages -------------------
        "ui.start_welcome": (
            "Привет! Я перешлю твои сообщения в Claude Code. Просто отправь мне сообщение."
        ),
        "ui.thinking": "⏳ Думаю...",
        "ui.recognizing_voice": "⏳ Распознаю голосовое...",
        "ui.processing_forwards": "⏳ Обрабатываю пересланные сообщения...",
        "ui.processing_files": "⏳ Обрабатываю файлы...",
        "ui.new_session": (
            "Следующее сообщение создаст новый чат. Ищи его в «Недавних» "
            "Codex Desktop или через /resume."
        ),
        "ui.codex_chat_created": (
            "🆕 Чат Codex Desktop: <b>{title}</b> в проекте <code>{project}</code> "
            "(ID <code>{sid}</code>). Ищи в «Недавних» или первым в /resume."
        ),
        "ui.context_cleared": "🧹 Контекст очищен, tmux-сессия жива",
        "ui.reset_failed": "❌ Не удалось обновить контекст. Попробуй ещё раз через пару секунд.",
        "ui.topic_welcome": (
            "👋 Тема готова. Скажи, для чего она — какой проект (путь или название) и "
            "нужен ли кастомный промпт."
        ),
        "ui.cancelled": "❌ Отменено",
        "ui.nothing_to_cancel": "Нечего отменять",
        "ui.language_current": "Язык: <b>{lang}</b>. Используй /language ru или /language en.",
        "ui.language_changed": "Язык переключён на <b>{lang}</b>.",
        "ui.language_invalid": "Неизвестный язык. Используй /language ru или /language en.",
        "ui.already_finished": "Процесс уже завершён",
        "ui.tmux_failed": "❌ Не удалось запустить tmux: {exc}",
        "ui.tmux_killed": "🗑 Tmux-сессия убита",
        "ui.subprocess_killed": "🗑 Сессия остановлена. Следующее сообщение начнёт новый чат.",
        "ui.subprocess_recycled": (
            "🔄 Текущая задача остановлена. Следующее сообщение продолжит эту сессию."
        ),
        "ui.tmux_not_active": "В этом топике нет активной tmux-сессии",
        "ui.recycle_done": "♻️ Runtime перезапущен",
        "ui.recycle_failed": (
            "❌ Не удалось перезапустить runtime. Попробуй ещё раз через пару секунд."
        ),
        "ui.engine_starting": (
            "🔄 {engine} запускается — подожди несколько секунд перед отправкой."
        ),
        "ui.engine_ready": "✅ {engine} готов к работе.",
        "ui.engine_start_failed": "⚠️ {engine} не запустился: {exc}",
        "ui.engine_started_input_blocked": (
            "⚠️ {engine} запустился, но ввод заблокирован — скорее всего модальное окно."
            " Открой /tui, чтобы увидеть экран сессии и закрыть его."
        ),
        "ui.codex_update_status": (
            "Статус обновления Codex: <b>{status}</b>\n"
            "Последний успех: <code>{last_success}</code>\n"
            "{output}"
        ),
        "ui.codex_update_running": "🔄 Обновляю Codex...",
        "ui.codex_update_success": "✅ Codex обновлён.\n<pre>{output}</pre>",
        "ui.codex_update_failed": "❌ Обновление Codex упало ({status}).\n<pre>{output}</pre>",
        "ui.codex_update_already_running": "Обновление Codex уже идёт.",
        "ui.codex_update_active_sessions": (
            "⚠️ Обновление Codex пропущено: сначала останови активные Codex-сессии через /kill."
        ),
        "ui.codex_update_cooldown": "Автообновление Codex пропущено: cooldown ещё активен.",
        # --- UI: tail / tui feature strings (Wave 3 tmux-tui-mode) ----
        "ui.tail_unavailable": (
            "⚠️ Нет активной tmux-сессии — /tui недоступен."
            " Сначала переключись в tmux или запусти сессию."
        ),
        "ui.tail_snapshot_header": "Снимок TUI (последние строки):",
        "ui.tail_keyboard_stale": (
            "Клавиатура устарела — сессия перезапущена. Вызови /tui заново."
        ),
        "ui.modal_blocked_header": (
            "⚠️ Сообщение НЕ отправлено — CC ждёт действие в модальном диалоге.\n"
            "Твоё сообщение: <code>{prompt}</code>\n"
            "Закрой диалог (Esc / выбери пункт) и отправь заново."
        ),
        "ui.delivery_unconfirmed_header": (
            "⚠️ Не удалось подтвердить отправку — сообщение уже может находиться "
            "в поле ввода Codex.\n"
            "Твоё сообщение: <code>{prompt}</code>\n"
            "Открой /tui и проверь поле ввода. Пока не отправляй сообщение повторно."
        ),
        "ui.modal_idle_detected": (
            "⚠️ CC ждёт действие в модальном диалоге.\n"
            "Закрой его (Esc / выбери пункт), чтобы CC продолжил работу."
        ),
        "ui.tui_start_timeout": (
            "❌ Claude Code TUI не поднялся за 30 с — tmux убит."
            " Попробуй ещё раз или переключись /mode → обычный."
        ),
        "ui.tui_session_missing": (
            "Эта сессия недоступна (создана до миграции) — пиши в текущую сессию дальше."
        ),
        # --- UI: exec-mode picker -------------------------------------
        "ui.exec_mode_picker_caption": (
            "Режим выполнения: <b>{current}</b>\n\n"
            "⚡ обычный — каждое сообщение запускает сабпроцесс Claude без"
            " контекста от прошлых сообщений. Задача выполняется, сессия"
            " умирает, ресурсы не жрёт. Подходит для разовых задач, заметок"
            " и т.п.\n\n"
            "🖥 tmux — персистентная tmux-сессия, которая постоянно живёт и"
            " помнит контекст между сообщениями. Это полноценный Claude Code,"
            " поддерживает agent team, но постоянно жрёт ресурсы. Лучше для"
            " разработки и больших задач. Первый запуск ~1-2 сек. Чтобы убить"
            " сессию и освободить ресурсы — команда /kill."
        ),
        "ui.exec_mode_changed": "Режим: {mode}",
        "ui.exec_mode_already": "Уже: {mode}",
        "ui.exec_mode_busy": "Сейчас идёт обработка, нажми /cancel и повтори",
        "ui.exec_mode_invalid": "Неизвестный режим",
        "ui.exec_mode_not_in_forum": "⚠️ /mode работает только в форум-топиках",
        "ui.exec_mode_write_failed": "Не удалось сохранить режим, попробуй ещё раз",
        "ui.exec_mode_label_subprocess": "⚡ обычный",
        "ui.exec_mode_label_tmux": "🖥 tmux",
        "ui.engine_picker_caption": (
            "Движок: <b>{engine}</b>\n\n/mode остаётся режимом транспорта: обычный или tmux."
        ),
        "ui.engine_changed": "Движок: {engine}",
        "ui.engine_changed_new_session": (
            "Движок — {engine}.\n\nАктивная сессия сброшена. Следующее сообщение"
            " запустит новую сессию с {engine}."
        ),
        "ui.engine_already": "Уже выбрано",
        "ui.engine_invalid": "Неизвестный движок",
        "ui.engine_not_in_forum": "⚠️ /engine работает только внутри форум-топиков",
        "ui.engine_write_failed": "Не удалось сохранить настройки движка",
        "ui.tmux_started_engine": "🖥 Создана новая tmux-сессия с {engine}",
        "ui.reply_engine_switched": ("↪️ Ответ ведёт в {engine}; переключаю топик перед resume."),
        "ui.stream_mode_picker_caption": (
            "Режим трансляции этого топика: <b>{current}</b>\n\n"
            "• <b>verbose</b> — каждое событие отдельным сообщением\n"
            "• <b>live</b> — один редактируемый буфер с прогрессом\n"
            "• <b>minimal</b> — только финальные ответы"
        ),
        "ui.stream_mode_changed": "Режим: {mode}",
        "ui.stream_mode_invalid": "Неизвестный режим",
        "ui.stream_mode_not_in_forum": "⚠️ /stream работает только внутри форум-топиков.",
        "ui.stream_mode_write_failed": "Не удалось записать конфиг",
        "ui.claude_no_text": "Claude завершил ход без текстового ответа.",
        "ui.claude_stop_sequence_no_text": (
            "Claude завершил ход по стоп-последовательности без текстового ответа."
        ),
        "ui.claude_limit_no_text": "Claude достиг лимита вывода без текстового ответа.",
        "ui.claude_context_no_text": "Claude достиг лимита контекста без текстового ответа.",
        "ui.claude_refused_no_text": "Claude отказался отвечать.",
        "ui.claude_unknown_completion": ("Claude завершил предыдущий ход в неизвестном формате."),
        "ui.claude_limit_note": "\n\n⚠️ Ответ оборван из-за лимита вывода.",
        "ui.claude_context_note": "\n\n⚠️ Ответ оборван из-за лимита контекста.",
        "ui.session_switched": "🔄 сессия: {sid}",
        "ui.session_switched_engine": "🔄 сессия {engine}: {sid}",
        "ui.resume_picker_caption_hdr": "Сессии для <code>{cwd}</code>, страница {page}/{total}",
        "ui.resume_picker_caption_all_hdr": (
            "Сохранённые чаты всех проектов, страница {page}/{total}. "
            "Если текущий чат есть в списке, он первый и отмечен ✅. "
            "Поиск по названию или проекту: /resume &lt;слово&gt;"
        ),
        "ui.resume_no_sessions": "Сохранённых сессий для этого cwd нет",
        "ui.resume_not_in_forum": "⚠️ /resume работает только внутри форум-топиков",
        "ui.resume_subprocess_unsupported": "Команда работает только в tmux-режиме (/mode)",
        "ui.resume_already_on_it": "Уже на этой сессии",
        "ui.resume_target_missing": "Транскрипт пропал; открой /resume заново",
        "ui.resume_invalid_id": "Некорректный ID сессии",
        "ui.resume_starting": "Возобновляю...",
        "ui.resume_current_marker": "текущая",
        "ui.resume_switched": "🔄 сессия: <code>{sid}</code>",
        "ui.resume_selection_details": (
            "Чат: <b>{title}</b>\nПроект: <code>{cwd}</code>\n"
            "Отправьте сообщение, чтобы продолжить."
        ),
        "ui.resume_started": "🆕 tmux поднят с resume <code>{sid}</code>",
        "ui.resume_engine_switched": "↪️ Переключаю движок на <code>{engine}</code>",
        "ui.resume_picker_stale": "Список устарел, открой /resume заново",
        "ui.resume_spawn_failed": (
            "Не удалось поднять tmux. Следующее сообщение начнёт fresh-сессию"
        ),
        "ui.resume_spawn_failed_engine_changed": (
            "Не удалось поднять tmux. Движок уже переключён на {engine}; "
            "следующее сообщение начнёт fresh-сессию."
        ),
        "ui.resume_config_write_failed": "Не удалось обновить engine; ничего не изменено",
        "ui.resume_cancelled": "Отменено",
        "ui.error_generic": "Произошла ошибка при обработке запроса. Попробуй ещё раз.",
        "ui.cc_not_found": (
            "Claude Code не найден. Убедитесь, что он установлен и доступен в PATH."
        ),
        "ui.agent_cli_not_found": (
            "Я не нашёл ни Claude Code, ни Codex. Установи хотя бы один из них "
            "для того же Linux-пользователя, который запускает бота, проверь PATH "
            "и перезапусти бота."
        ),
        "ui.inactivity_kill": "Зависло, попробуй ещё раз",
        "ui.voice_too_large": "Голосовое сообщение слишком большое (максимум 100 МБ).",
        "ui.voice_not_recognized": "Не удалось распознать голосовое сообщение",
        "ui.download_error": "Не удалось скачать файл, попробуй ещё раз",
        "ui.file_too_large": "Файл слишком большой (максимум {size} МБ)",
        "ui.forward_error": "Не удалось обработать сообщения, попробуй ещё раз",
        # --- UI: keyboard buttons -------------------------------------
        "ui.btn_new_chat": "Новый чат",
        "ui.btn_cancel": "Отменить ❌",
        "ui.btn_tui": "TUI 🖥",
        # CC content prefixes are intentionally English-only — see MESSAGES["en"].
        # --- Queue messages -------------------------------------------
        "ui.queue_added_batch": "Добавлено в батч, он №{position} в очереди",
        "ui.queue_added": "Добавлено в очередь (№{position})",
        "ui.queue_session_suffix": ", сессия: {sid}",
        "ui.queue_item_dropped": (
            "⚠️ Не удалось обработать сообщение — оно не дошло до агента. Отправь ещё раз."
        ),
        "ui.queue_dropped_shutdown": (
            "⚠️ Бот перезапускается — сообщение из очереди не обработано. "
            "Отправь его ещё раз после рестарта."
        ),
        # --- Tool status ----------------------------------------------
        "tool.read": "📖 Читаю файл",
        "tool.grep": "🔍 Ищу",
        "tool.glob": "🔍 Ищу файлы",
        "tool.bash": "⚙️ Выполняю",
        "tool.bash_with_cmd": "⚙️ Выполняю: {cmd}",
        "tool.write": "✏️ Пишу файл",
        "tool.edit": "✏️ Редактирую",
        "tool.skill": "📋 Загружаю Skill",
        "tool.agent": "🤖 Запускаю субагента",
        "tool.agent_done": "✅ Субагент завершил работу",
        "tool.agent_done_with_desc": "✅ Субагент завершил работу: {desc}",
        "tool.send_message": "💬 Отправляю сообщение...",
        "tool.send_image": "🖼 Отправляю картинку...",
        "tool.send_image_gallery": "🖼 Отправляю галерею...",
        "tool.send_document": "📎 Отправляю документ...",
        "tool.fetch_url": "🌐 Загружаю URL",
        "tool.run_tests": "🧪 Запускаю тесты",
        "tool.calc_time": "🧮 Считаю время",
        "tool.check_time": "🕐 Проверяю время",
        "tool.read_memory": "🧠 Читаю память",
        "tool.write_memory": "🧠 Обновляю память",
        "tool.read_skill": "📋 Читаю скилл",
        "tool.write_skill": "📋 Обновляю скилл",
    },
}


@functools.lru_cache(maxsize=1)
def _get_lang() -> str:
    """Read BOT_LANG from environment once per process.

    Reads os.environ directly (not Settings) so that t() works at module
    import time before .env validation runs — handlers use t() in router
    filters, which fire while the dispatcher is being built.
    """
    import os

    lang = os.environ.get("BOT_LANG", _DEFAULT_LANG)
    if lang not in MESSAGES:
        return _DEFAULT_LANG
    return lang


def t(key: str, **kwargs: Any) -> str:
    """Translate a key using the configured bot language.

    Falls back to English if the key is missing in the active language,
    then to the key itself if missing in English too. Format placeholders
    via kwargs.
    """
    lang = _get_lang()
    template = MESSAGES.get(lang, MESSAGES[_DEFAULT_LANG]).get(key)
    if template is None:
        template = MESSAGES[_DEFAULT_LANG].get(key, key)
    if kwargs:
        # t() is used on error paths — a template/kwargs mismatch (stray
        # brace, renamed placeholder) must degrade to the raw template, not
        # raise and mask the original error.
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            logger.warning("t(%s): format failed with kwargs %s", key, sorted(kwargs))
            return template
    return template


def reset_lang_cache() -> None:
    """Clear cached language — for tests that change settings between runs."""
    _get_lang.cache_clear()
