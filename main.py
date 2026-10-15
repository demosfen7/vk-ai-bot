"""Точка входа: python main.py"""

from __future__ import annotations

import logging
import sys

import truststore

# Доверять системным сертификатам ОС, а не только встроенному списку Python.
# Нужно, если HTTPS проверяет антивирус (например, Касперский подменяет сертификат
# api.openai.com своим). Вызывается до импорта vkbottle/aiohttp и httpx:
# aiohttp создаёт SSL-контекст уже при импорте.
truststore.inject_into_ssl()

from bot.config import USERS_DIR, ConfigError, load_config  # noqa: E402
from bot.llm_client import LLMClient  # noqa: E402
from bot.memory import DialogMemory  # noqa: E402
from bot.vk_client import BotService, create_bot  # noqa: E402


def setup_logging() -> None:
    # Консоль Windows может быть в cp1252 — без этого кириллица в логах вызывает ошибки.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # У VKBottle свой цветной обработчик — без этого его строки в логе дублируются.
    logging.getLogger("vkbottle").handlers.clear()
    # httpx пишет в INFO каждый запрос — это лишнее.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    logger = logging.getLogger("main")

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    service = BotService(
        memory=DialogMemory(USERS_DIR),
        llm=LLMClient(api_key=config.openai_api_key, model=config.openai_model),
    )
    bot = create_bot(config, service)

    logger.info("Бот запускается: сообщество %s, модель %s", config.vk_group_id, config.openai_model)
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("Бот остановлен")


if __name__ == "__main__":
    main()
