"""Память пользователей: несколько диалогов на пользователя, один JSON-файл в users/."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

from bot.config import HISTORY_LIMIT
from bot.modes import DEFAULT_MODE, is_valid_mode

logger = logging.getLogger(__name__)

# Лимит ВКонтакте: в inline-клавиатуре не больше 10 кнопок,
# а в списке диалогов кроме самих диалогов ещё 4 кнопки управления.
MAX_DIALOGS = 6
MAX_TITLE_LENGTH = 30
MAIN_DIALOG_TITLE = "Основной диалог"


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DialogLimitError(Exception):
    """Нельзя создать диалог: достигнут MAX_DIALOGS."""


@dataclass
class Dialog:
    id: int
    title: str
    mode: str = DEFAULT_MODE
    history: list[ChatMessage] = field(default_factory=list)
    created_at: str = field(default_factory=_now)


@dataclass
class UserState:
    """Всё, что бот помнит о пользователе. Токенов и ключей здесь нет."""

    dialogs: list[Dialog]
    active_id: int
    next_id: int
    # Незавершённое действие, например {"action": "rename", "dialog_id": 2}.
    pending: dict[str, Any] | None = None
    updated_at: str = field(default_factory=_now)

    @property
    def active(self) -> Dialog:
        dialog = self.get(self.active_id)
        if dialog is None:  # не должно случаться, но файл могли поправить руками
            dialog = self.dialogs[0]
            self.active_id = dialog.id
        return dialog

    def get(self, dialog_id: int) -> Dialog | None:
        return next((d for d in self.dialogs if d.id == dialog_id), None)

    def create_dialog(self, title: str | None = None) -> Dialog:
        """Создаёт диалог и делает его активным."""
        if len(self.dialogs) >= MAX_DIALOGS:
            raise DialogLimitError
        dialog = Dialog(id=self.next_id, title=title or f"Диалог {self.next_id}")
        self.next_id += 1
        self.dialogs.append(dialog)
        self.active_id = dialog.id
        return dialog

    def delete_dialog(self, dialog_id: int) -> None:
        """Удаляет диалог. Последний удалённый заменяется пустым «Основным диалогом»."""
        self.dialogs = [d for d in self.dialogs if d.id != dialog_id]
        if not self.dialogs:
            self.dialogs.append(Dialog(id=self.next_id, title=MAIN_DIALOG_TITLE))
            self.next_id += 1
        if self.get(self.active_id) is None:
            self.active_id = self.dialogs[0].id

    def add_exchange(self, user_text: str, assistant_text: str, limit: int) -> None:
        """Добавляет в активный диалог пару «вопрос — ответ» и обрезает историю до limit."""
        history = self.active.history
        history.append(ChatMessage(role="user", content=user_text))
        history.append(ChatMessage(role="assistant", content=assistant_text))
        del history[:-limit]

    @classmethod
    def new(cls) -> UserState:
        return cls(dialogs=[Dialog(id=1, title=MAIN_DIALOG_TITLE)], active_id=1, next_id=2)


class DialogMemory:
    """Хранит состояние каждого пользователя в users/{user_id}.json."""

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
        """Загружает состояние; если файла нет или он повреждён — новое состояние."""
        path = self.path(user_id)
        if not path.exists():
            return UserState.new()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return self._parse(data)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.warning("Не удалось прочитать %s (%s), начинаю заново", path.name, exc)
            return UserState.new()

    def save(self, user_id: int, state: UserState) -> None:
        """Сохраняет состояние атомарно: пишет во временный файл и заменяет им основной."""
        for dialog in state.dialogs:
            del dialog.history[: -self.limit]
        state.updated_at = _now()
        path = self.path(user_id)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    def _parse(self, data: dict[str, Any]) -> UserState:
        if "dialogs" not in data:
            # Старый формат (один диалог): {"mode", "history", "updated_at"}.
            mode = data.get("mode", DEFAULT_MODE)
            dialog = Dialog(
                id=1,
                title=MAIN_DIALOG_TITLE,
                mode=mode if is_valid_mode(mode) else DEFAULT_MODE,
                history=self._parse_history(data.get("history", [])),
            )
            return UserState(dialogs=[dialog], active_id=1, next_id=2)

        dialogs = [
            Dialog(
                id=int(d["id"]),
                title=str(d.get("title") or f"Диалог {d['id']}")[:MAX_TITLE_LENGTH],
                mode=d.get("mode") if is_valid_mode(d.get("mode", "")) else DEFAULT_MODE,
                history=self._parse_history(d.get("history", [])),
                created_at=str(d.get("created_at") or _now()),
            )
            for d in data["dialogs"]
        ][:MAX_DIALOGS]
        if not dialogs:
            return UserState.new()
        pending = data.get("pending")
        return UserState(
            dialogs=dialogs,
            active_id=int(data.get("active_id", dialogs[0].id)),
            next_id=max(int(data.get("next_id", 0)), max(d.id for d in dialogs) + 1),
            pending=pending if isinstance(pending, dict) else None,
            updated_at=str(data.get("updated_at", "")),
        )

    def _parse_history(self, raw: Any) -> list[ChatMessage]:
        history = [
            ChatMessage(role=m["role"], content=m["content"])
            for m in raw
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        ]
        return history[-self.limit :]
