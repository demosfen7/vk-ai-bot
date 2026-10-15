"""Подключение к ВКонтакте (Bots Long Poll API) и обработка личных сообщений."""

from __future__ import annotations

import logging

from vkbottle import API, BotPolling, VKAPIError
from vkbottle.bot import Bot, Message

from bot.config import Config
from bot.llm_client import LLMClient, LLMError
from bot.memory import DialogMemory
from bot.modes import MODES, get_mode, get_system_prompt, is_valid_mode, modes_help

logger = logging.getLogger(__name__)

# Лимит ВКонтакте — 4096 символов на сообщение, берём с запасом.
VK_MESSAGE_LIMIT = 4000

TEMPORARY_ERROR_TEXT = (
    "Извините, сейчас не получается ответить — временная техническая проблема. "
    "Попробуйте, пожалуйста, чуть позже."
)
NON_TEXT_TEXT = "Пока я понимаю только текстовые сообщения. Напишите, пожалуйста, текстом 🙂"


class BotService:
    """Логика бота без привязки к VK: команды, память и вызов LLM."""

    def __init__(self, memory: DialogMemory, llm: LLMClient) -> None:
        self.memory = memory
        self.llm = llm

    async def handle_text(self, user_id: int, text: str) -> str:
        """Возвращает ответ на текстовое сообщение пользователя."""
        async with self.memory.lock(user_id):
            if text.startswith("/"):
                return self.handle_command(user_id, text)
            return await self._ask_llm(user_id, text)

    def handle_command(self, user_id: int, text: str) -> str:
        """Команды обрабатываются локально и в LLM не отправляются."""
        command, _, argument = text.strip().partition(" ")
        command = command.lower()
        argument = argument.strip().lower()

        if command == "/start":
            current = get_mode(self.memory.load(user_id)["mode"])
            return (
                "Привет! Я AI-бот этого сообщества. Задайте вопрос или просто поболтайте со мной — "
                "я помню контекст нашего диалога.\n\n"
                f"Текущий режим: {current.name} ({current.title}).\n\n"
                f"Режимы общения:\n{modes_help()}\n\n"
                "Другие команды:\n"
                "/mode — показать текущий режим\n"
                "/reset — очистить историю диалога"
            )

        if command == "/mode":
            if not argument:
                current = get_mode(self.memory.load(user_id)["mode"])
                return (
                    f"Текущий режим: {current.name} ({current.title}) — {current.description}.\n\n"
                    f"Сменить режим:\n{modes_help()}"
                )
            if not is_valid_mode(argument):
                return f"Не знаю режим «{argument}». Доступные режимы:\n{modes_help()}"
            self.memory.set_mode(user_id, argument)
            mode = MODES[argument]
            logger.info("Пользователь %s переключил режим на %s", user_id, mode.name)
            return f"Режим переключён: {mode.name} ({mode.title}) — {mode.description}."

        if command == "/reset":
            self.memory.reset(user_id)
            logger.info("Пользователь %s очистил историю", user_id)
            return "История диалога очищена. Начнём с чистого листа!"

        return (
            f"Неизвестная команда {command}. Доступные команды: /start, /mode, "
            "/mode assistant, /mode friend, /mode expert, /reset."
        )

    async def _ask_llm(self, user_id: int, text: str) -> str:
        state = self.memory.load(user_id)
        try:
            answer = await self.llm.complete(
                system_prompt=get_system_prompt(state["mode"]),
                history=state["history"],
                user_message=text,
            )
        except LLMError as exc:
            # В историю ничего не пишем, чтобы не копить вопросы без ответов.
            logger.error("Ошибка LLM для пользователя %s: %s", user_id, exc)
            return TEMPORARY_ERROR_TEXT
        self.memory.add_exchange(user_id, text, answer)
        return answer


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


def create_bot(config: Config, service: BotService) -> Bot:
    """Создаёт бота VKBottle, который слушает личные сообщения через Long Poll."""
    api = API(config.vk_token)
    bot = Bot(api=api, polling=BotPolling(api=api, group_id=config.vk_group_id))

    @bot.on.private_message()
    async def on_private_message(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            reply = NON_TEXT_TEXT
        else:
            try:
                reply = await service.handle_text(message.from_id, text)
            except Exception:
                # Одно неудачное сообщение не должно ронять бота.
                logger.exception("Ошибка при обработке сообщения от %s", message.from_id)
                reply = TEMPORARY_ERROR_TEXT

        for part in split_message(reply):
            try:
                await message.answer(part)
            except VKAPIError as exc:
                logger.error("ВКонтакте не принял ответ пользователю %s: %s", message.from_id, exc)
                return
            except Exception:
                logger.exception("Не удалось отправить ответ пользователю %s", message.from_id)
                return

    bot.on_startup.append(check_vk_access(api, config.vk_group_id))
    bot.on_shutdown.append(service.llm.aclose())
    return bot
