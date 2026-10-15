"""Загрузка настроек из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
USERS_DIR = PROJECT_ROOT / "users"

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_OPENAI_MODEL = "gpt-6-luna"
# Сколько последних сообщений (user + assistant) хранить и передавать в LLM.
HISTORY_LIMIT = 20

REQUIRED_VARS = ("VK_TOKEN", "VK_GROUP_ID", "OPENAI_API_KEY")


class ConfigError(Exception):
    """Настройки заданы неверно: бот не может стартовать."""


@dataclass(frozen=True)
class Config:
    # repr=False — чтобы секреты не попали в лог при случайном print(config).
    vk_token: str = field(repr=False)
    vk_group_id: int
    openai_api_key: str = field(repr=False)
    openai_model: str


def load_config() -> Config:
    """Читает .env и проверяет обязательные переменные.

    Raises:
        ConfigError: если переменная отсутствует или имеет неверный формат.
    """
    load_dotenv(ENV_FILE)

    values = {name: os.getenv(name, "").strip() for name in REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(
            "Не заданы обязательные переменные окружения: "
            + ", ".join(missing)
            + f". Скопируйте .env.example в .env и заполните их (файл: {ENV_FILE})."
        )

    group_id_raw = values["VK_GROUP_ID"].lstrip("-")
    if not group_id_raw.isdigit():
        raise ConfigError(
            f"VK_GROUP_ID должен быть числом (ID сообщества), а сейчас: {values['VK_GROUP_ID']!r}."
        )

    return Config(
        vk_token=values["VK_TOKEN"],
        vk_group_id=int(group_id_raw),
        openai_api_key=values["OPENAI_API_KEY"],
        openai_model=os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL,
    )
