"""Краткосрочная память диалогов: один JSON-файл на пользователя в users/."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

from bot.config import HISTORY_LIMIT
from bot.modes import DEFAULT_MODE, is_valid_mode

logger = logging.getLogger(__name__)


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class UserState(TypedDict):
    mode: str
    history: list[ChatMessage]
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DialogMemory:
    """Хранит историю и режим каждого пользователя в users/{user_id}.json.

    В файлы попадают только сообщения, режим и время обновления —
    никаких токенов и ключей.
    """

    def __init__(self, users_dir: Path, limit: int = HISTORY_LIMIT) -> None:
        self.users_dir = users_dir
        self.limit = limit
        self.users_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, user_id: int) -> asyncio.Lock:
        """Блокировка на пользователя: два его сообщения подряд не затрут файл друг другу."""
        return self._locks.setdefault(user_id, asyncio.Lock())

    def path(self, user_id: int) -> Path:
        return self.users_dir / f"{user_id}.json"

    def load(self, user_id: int) -> UserState:
        """Загружает состояние; если файла нет или он повреждён — чистое состояние."""
        path = self.path(user_id)
        if not path.exists():
            return self._empty_state()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось прочитать %s (%s), начинаю диалог заново", path.name, exc)
            return self._empty_state()

        mode = data.get("mode", DEFAULT_MODE)
        history = [
            ChatMessage(role=m["role"], content=m["content"])
            for m in data.get("history", [])
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        ]
        return UserState(
            mode=mode if is_valid_mode(mode) else DEFAULT_MODE,
            history=history[-self.limit :],
            updated_at=str(data.get("updated_at", "")),
        )

    def save(self, user_id: int, state: UserState) -> None:
        """Сохраняет состояние атомарно: пишет во временный файл и заменяет им основной."""
        state["history"] = state["history"][-self.limit :]
        state["updated_at"] = _now()
        path = self.path(user_id)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    def add_exchange(self, user_id: int, user_text: str, assistant_text: str) -> None:
        """Добавляет в историю пару «вопрос пользователя — ответ ассистента»."""
        state = self.load(user_id)
        state["history"].append(ChatMessage(role="user", content=user_text))
        state["history"].append(ChatMessage(role="assistant", content=assistant_text))
        self.save(user_id, state)

    def set_mode(self, user_id: int, mode: str) -> None:
        state = self.load(user_id)
        state["mode"] = mode
        self.save(user_id, state)

    def reset(self, user_id: int) -> None:
        """Очищает историю, режим общения сохраняется."""
        state = self.load(user_id)
        state["history"] = []
        self.save(user_id, state)

    @staticmethod
    def _empty_state() -> UserState:
        return UserState(mode=DEFAULT_MODE, history=[], updated_at=_now())
