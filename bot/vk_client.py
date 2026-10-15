"""Подключение к ВКонтакте (Bots Long Poll API) и обработка личных сообщений."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from vkbottle import API, BotPolling, VKAPIError
from vkbottle.bot import Bot, Message

from bot import keyboards
from bot.config import Config
from bot.llm_client import LLMClient, LLMError
from bot.memory import MAX_DIALOGS, MAX_TITLE_LENGTH, DialogLimitError, DialogMemory, UserState
from bot.modes import MODES, get_mode, get_system_prompt, is_valid_mode, modes_help

logger = logging.getLogger(__name__)

# Лимит ВКонтакте — 4096 символов на сообщение, берём с запасом.
VK_MESSAGE_LIMIT = 4000

TEMPORARY_ERROR_TEXT = (
    "Извините, сейчас не получается ответить — временная техническая проблема. "
    "Попробуйте, пожалуйста, чуть позже."
)
NON_TEXT_TEXT = "Пока я понимаю только текстовые сообщения. Напишите, пожалуйста, текстом 🙂"

HELP_TEXT = (
    "❓ Как пользоваться ботом\n\n"
    "Просто напишите сообщение — я отвечу с учётом истории активного диалога.\n\n"
    "Кнопки меню:\n"
    "➕ Новый диалог — начать разговор с чистого листа (старые диалоги сохранятся)\n"
    "📋 Список диалогов — переключиться, переименовать или удалить диалог\n"
    "🎭 Сменить режим — выбрать стиль общения для активного диалога\n"
    "⚙️ Настройки — сводка и очистка истории диалога\n\n"
    f"У каждого диалога свой режим и своя память (последние сообщения). Диалогов — до {MAX_DIALOGS}.\n\n"
    "Команды текстом:\n"
    "/start — приветствие\n"
    "/mode — текущий режим, /mode <режим> — сменить\n"
    "/reset — очистить историю активного диалога\n\n"
    f"Режимы:\n{modes_help()}"
)


@dataclass
class Reply:
    text: str
    keyboard: str | None = None


class BotService:
    """Логика бота без привязки к VK: кнопки, команды, диалоги и вызов LLM."""

    def __init__(self, memory: DialogMemory, llm: LLMClient) -> None:
        self.memory = memory
        self.llm = llm

    async def handle(self, user_id: int, text: str, payload: dict[str, Any] | None = None) -> Reply:
        """Возвращает ответ на сообщение пользователя (текст или нажатие кнопки)."""
        async with self.memory.lock(user_id):
            first_contact = not self.memory.path(user_id).exists()
            state = self.memory.load(user_id)

            button = self._detect_button(text, payload)
            if button is not None:
                state.pending = None
                reply = self._handle_button(state, *button)
            elif text.startswith("/"):
                state.pending = None
                reply = self._handle_command(state, text)
            elif state.pending and state.pending.get("action") == "rename":
                reply = self._finish_rename(state, text)
            else:
                reply = await self._ask_llm(user_id, state, text)
                if first_contact and reply.keyboard is None:
                    reply.keyboard = keyboards.main_menu()

            self.memory.save(user_id, state)
            return reply

    # --- кнопки ---------------------------------------------------------------

    @staticmethod
    def _detect_button(text: str, payload: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
        if isinstance(payload, dict):
            if isinstance(payload.get("cmd"), str):
                return payload["cmd"], payload
            if payload.get("command") == "start":  # кнопка «Начать» ВКонтакте
                return "start", payload
        if text in keyboards.MAIN_MENU_LABELS:
            return keyboards.MAIN_MENU_LABELS[text], {}
        return None

    def _handle_button(self, state: UserState, cmd: str, data: dict[str, Any]) -> Reply:
        dialog_id = data.get("id")
        target = state.get(dialog_id) if isinstance(dialog_id, int) else None

        if cmd == "start":
            return self._start(state)
        if cmd == "menu":
            return Reply(f"🏠 Главное меню\n\n{self._status(state)}", keyboards.main_menu())
        if cmd == "help":
            return Reply(HELP_TEXT, keyboards.main_menu())
        if cmd == "new":
            return self._new_dialog(state)
        if cmd == "list":
            return Reply(self._status(state), keyboards.dialogs_list(state))
        if cmd == "modes":
            return Reply(
                f"🎭 Выберите режим общения для диалога «{state.active.title}»:",
                keyboards.modes(state.active.mode),
            )
        if cmd == "mode":
            return self._set_mode(state, str(data.get("mode", "")))
        if cmd == "settings":
            return Reply(self._settings_text(state), keyboards.settings())
        if cmd == "clear":
            return Reply(
                f"🧹 Очистить историю диалога «{state.active.title}»? Бот забудет этот разговор.",
                keyboards.confirm("🧹 Да, очистить", "clear_yes", "settings"),
            )
        if cmd == "clear_yes":
            state.active.history.clear()
            return Reply(f"🧹 История диалога «{state.active.title}» очищена.", keyboards.main_menu())
        if cmd == "rename_pick":
            return Reply("✏️ Какой диалог переименовать?", keyboards.pick_dialog(state, "rename"))
        if cmd == "delete_pick":
            return Reply("🗑 Какой диалог удалить?", keyboards.pick_dialog(state, "delete"))

        if cmd in ("open", "rename", "delete", "delete_yes") and target is None:
            return Reply(f"Этого диалога уже нет.\n\n{self._status(state)}", keyboards.dialogs_list(state))
        if cmd == "open":
            state.active_id = target.id
            return Reply(self._status(state), keyboards.dialogs_list(state))
        if cmd == "rename":
            state.pending = {"action": "rename", "dialog_id": target.id}
            return Reply(
                f"✏️ Введите новое название для «{target.title}» (до {MAX_TITLE_LENGTH} символов):",
                keyboards.cancel_only(),
            )
        if cmd == "delete":
            return Reply(
                f"🗑 Удалить диалог «{target.title}» вместе с историей? Отменить это будет нельзя.",
                keyboards.confirm("🗑 Да, удалить", "delete_yes", "list", id=target.id),
            )
        if cmd == "delete_yes":
            state.delete_dialog(target.id)
            logger.info("Удалён диалог %s", target.id)
            return Reply(f"🗑 Диалог «{target.title}» удалён.\n\n{self._status(state)}", keyboards.dialogs_list(state))

        return Reply(f"🏠 Главное меню\n\n{self._status(state)}", keyboards.main_menu())

    # --- текстовые команды ----------------------------------------------------

    def _handle_command(self, state: UserState, text: str) -> Reply:
        """Команды обрабатываются локально и в LLM не отправляются."""
        command, _, argument = text.strip().partition(" ")
        command = command.lower()
        argument = argument.strip().lower()

        if command == "/start":
            return self._start(state)
        if command == "/mode":
            if not argument:
                mode = get_mode(state.active.mode)
                return Reply(
                    f"Режим диалога «{state.active.title}»: {mode.title} — {mode.description}.\n\n"
                    f"Сменить режим:\n{modes_help()}",
                    keyboards.modes(state.active.mode),
                )
            return self._set_mode(state, argument)
        if command == "/reset":
            state.active.history.clear()
            return Reply(f"История диалога «{state.active.title}» очищена. Начнём с чистого листа!")
        return Reply(
            f"Неизвестная команда {command}. Доступные команды: /start, /mode, "
            f"/mode <режим>, /reset. Режимы: {', '.join(MODES)}."
        )

    # --- действия ---------------------------------------------------------------

    def _start(self, state: UserState) -> Reply:
        return Reply(
            "👋 Привет! Я AI-бот этого сообщества. Задайте вопрос или просто поболтайте со мной — "
            "я помню контекст разговора.\n\n"
            "Можно вести несколько диалогов, у каждого свой режим общения:\n"
            + "\n".join(f"— {m.title}: {m.description}" for m in MODES.values())
            + f"\n\n{self._status(state)}\n\nКнопки меню — внизу чата. Подробности — «❓ Помощь».",
            keyboards.main_menu(),
        )

    def _new_dialog(self, state: UserState) -> Reply:
        try:
            dialog = state.create_dialog()
        except DialogLimitError:
            return Reply(
                f"Можно вести не больше {MAX_DIALOGS} диалогов. Удалите ненужный и создайте новый.",
                keyboards.dialogs_list(state),
            )
        return Reply(
            f"➕ Создан «{dialog.title}», теперь он активный.\n"
            f"Режим: {get_mode(dialog.mode).title}. Пишите!",
            keyboards.main_menu(),
        )

    def _set_mode(self, state: UserState, mode_name: str) -> Reply:
        if not is_valid_mode(mode_name):
            return Reply(f"Не знаю режим «{mode_name}». Доступные режимы:\n{modes_help()}")
        state.active.mode = mode_name
        mode = MODES[mode_name]
        return Reply(
            f"🎭 Режим диалога «{state.active.title}»: {mode.title} — {mode.description}.",
            keyboards.main_menu(),
        )

    def _finish_rename(self, state: UserState, text: str) -> Reply:
        dialog = state.get(state.pending.get("dialog_id")) if state.pending else None
        if dialog is None:
            state.pending = None
            return Reply("Этот диалог уже удалён.", keyboards.dialogs_list(state))
        title = " ".join(text.split())
        if not title or len(title) > MAX_TITLE_LENGTH:
            return Reply(
                f"Название должно быть от 1 до {MAX_TITLE_LENGTH} символов. Попробуйте ещё раз:",
                keyboards.cancel_only(),
            )
        old_title, dialog.title = dialog.title, title
        state.pending = None
        return Reply(f"✏️ «{old_title}» → «{title}»\n\n{self._status(state)}", keyboards.dialogs_list(state))

    @staticmethod
    def _status(state: UserState) -> str:
        return f"✅ Активный диалог: «{state.active.title}»\nРежим: {get_mode(state.active.mode).title}"

    def _settings_text(self, state: UserState) -> str:
        return (
            "⚙️ Настройки\n\n"
            f"{self._status(state)}\n"
            f"Сообщений в памяти диалога: {len(state.active.history)} из {self.memory.limit}\n"
            f"Диалогов: {len(state.dialogs)} из {MAX_DIALOGS}\n"
            f"Модель: {self.llm.model}"
        )

    async def _ask_llm(self, user_id: int, state: UserState, text: str) -> Reply:
        dialog = state.active
        try:
            answer = await self.llm.complete(
                system_prompt=get_system_prompt(dialog.mode),
                history=dialog.history,
                user_message=text,
            )
        except LLMError as exc:
            # В историю ничего не пишем, чтобы не копить вопросы без ответов.
            logger.error("Ошибка LLM для пользователя %s: %s", user_id, exc)
            return Reply(TEMPORARY_ERROR_TEXT)
        state.add_exchange(text, answer, self.memory.limit)
        return Reply(answer)


def split_message(text: str, limit: int = VK_MESSAGE_LIMIT) -> list[str]:
    """Режет длинный ответ на части не длиннее limit, стараясь резать по переносам строк."""
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


async def check_vk_access(api: API, group_id: int) -> None:
    """Проверяет при старте, что токен рабочий и принадлежит сообществу VK_GROUP_ID."""
    try:
        groups = await api.groups.get_by_id()
    except VKAPIError as exc:
        logger.error(
            "ВКонтакте отклонил VK_TOKEN: %s. Проверьте ключ сообщества в .env.", exc
        )
        raise SystemExit(1) from exc

    group = groups.groups[0]
    if group.id != group_id:
        logger.error(
            "VK_TOKEN принадлежит сообществу %s (id %s), а в VK_GROUP_ID указан %s.",
            group.name, group.id, group_id,
        )
        raise SystemExit(1)
    logger.info("Подключено сообщество «%s» (id %s)", group.name, group.id)


def _payload(message: Message) -> dict[str, Any] | None:
    try:
        payload = message.get_payload_json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def create_bot(config: Config, service: BotService) -> Bot:
    """Создаёт бота VKBottle, который слушает личные сообщения через Long Poll."""
    api = API(config.vk_token)
    bot = Bot(api=api, polling=BotPolling(api=api, group_id=config.vk_group_id))

    @bot.on.private_message()
    async def on_private_message(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            reply = Reply(NON_TEXT_TEXT)
        else:
            try:
                reply = await service.handle(message.from_id, text, _payload(message))
            except Exception:
                # Одно неудачное сообщение не должно ронять бота.
                logger.exception("Ошибка при обработке сообщения от %s", message.from_id)
                reply = Reply(TEMPORARY_ERROR_TEXT)

        parts = split_message(reply.text)
        for i, part in enumerate(parts):
            keyboard = reply.keyboard if i == len(parts) - 1 else None
            try:
                await message.answer(part, keyboard=keyboard)
            except VKAPIError as exc:
                logger.error("ВКонтакте не принял ответ пользователю %s: %s", message.from_id, exc)
                return
            except Exception:
                logger.exception("Не удалось отправить ответ пользователю %s", message.from_id)
                return

    bot.on_startup.append(check_vk_access(api, config.vk_group_id))
    bot.on_shutdown.append(service.llm.aclose())
    return bot
