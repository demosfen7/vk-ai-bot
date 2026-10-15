"""Клавиатуры ВКонтакте: главное меню внизу чата и inline-кнопки под сообщениями.

Кнопки текстовые: нажатие отправляет в чат подпись кнопки, а скрытый payload
говорит боту, какая кнопка нажата. Payload вида {"cmd": "...", ...}.

Лимиты VK для inline-клавиатуры: не больше 10 кнопок и 6 рядов, подпись — до 40 символов.
"""

from __future__ import annotations

import json
from typing import Any

from vkbottle import Keyboard, KeyboardButtonColor, Text

from bot.memory import Dialog, UserState
from bot.modes import MODES

# Подписи кнопок главного меню. Если клиент не передал payload (старые приложения),
# бот распознаёт кнопку по подписи.
BTN_NEW = "➕ Новый диалог"
BTN_LIST = "📋 Список диалогов"
BTN_SETTINGS = "⚙️ Настройки"
BTN_MODES = "🎭 Сменить режим"
BTN_HELP = "❓ Помощь"

MAIN_MENU_LABELS = {
    BTN_NEW: "new",
    BTN_LIST: "list",
    BTN_SETTINGS: "settings",
    BTN_MODES: "modes",
    BTN_HELP: "help",
}

LABEL_LIMIT = 40


def _button(label: str, cmd: str, **data: Any) -> Text:
    payload = json.dumps({"cmd": cmd, **data}, ensure_ascii=False)
    if len(label) > LABEL_LIMIT:
        label = label[: LABEL_LIMIT - 1] + "…"
    return Text(label, payload=payload)


def main_menu() -> str:
    """Постоянная клавиатура внизу чата."""
    kb = Keyboard(one_time=False, inline=False)
    kb.add(_button(BTN_NEW, "new"))
    kb.add(_button(BTN_LIST, "list"))
    kb.row()
    kb.add(_button(BTN_SETTINGS, "settings"))
    kb.add(_button(BTN_MODES, "modes"))
    kb.row()
    kb.add(_button(BTN_HELP, "help"))
    return kb.get_json()


def _add_dialog_buttons(kb: Keyboard, dialogs: list[Dialog], cmd: str, active_id: int | None) -> None:
    """Кнопки диалогов: до 4 — по одной в ряд, больше — по две (лимит 6 рядов)."""
    per_row = 1 if len(dialogs) <= 4 else 2
    for i, dialog in enumerate(dialogs):
        if i and i % per_row == 0:
            kb.row()
        is_active = dialog.id == active_id
        label = f"✅ {dialog.title}" if is_active else dialog.title
        color = KeyboardButtonColor.POSITIVE if is_active else KeyboardButtonColor.SECONDARY
        kb.add(_button(label, cmd, id=dialog.id), color=color)


def dialogs_list(state: UserState) -> str:
    kb = Keyboard(inline=True)
    _add_dialog_buttons(kb, state.dialogs, "open", state.active_id)
    kb.row()
    kb.add(_button("✏️ Переименовать", "rename_pick"), color=KeyboardButtonColor.SECONDARY)
    kb.add(_button("🗑 Удалить", "delete_pick"), color=KeyboardButtonColor.NEGATIVE)
    kb.row()
    kb.add(_button("➕ Новый", "new"), color=KeyboardButtonColor.SECONDARY)
    kb.add(_button("🏠 Меню", "menu"), color=KeyboardButtonColor.PRIMARY)
    return kb.get_json()


def pick_dialog(state: UserState, cmd: str) -> str:
    """Выбор диалога для переименования или удаления."""
    kb = Keyboard(inline=True)
    _add_dialog_buttons(kb, state.dialogs, cmd, active_id=None)
    kb.row()
    kb.add(_button("◀️ Отмена", "list"), color=KeyboardButtonColor.PRIMARY)
    return kb.get_json()


def cancel_only() -> str:
    kb = Keyboard(inline=True)
    kb.add(_button("◀️ Отмена", "list"), color=KeyboardButtonColor.PRIMARY)
    return kb.get_json()


def confirm(yes_label: str, cmd: str, back_cmd: str, **data: Any) -> str:
    kb = Keyboard(inline=True)
    kb.add(_button(yes_label, cmd, **data), color=KeyboardButtonColor.NEGATIVE)
    kb.add(_button("◀️ Отмена", back_cmd), color=KeyboardButtonColor.PRIMARY)
    return kb.get_json()


def modes(current: str) -> str:
    """Шесть режимов по два в ряд + «Назад»."""
    kb = Keyboard(inline=True)
    for i, mode in enumerate(MODES.values()):
        if i and i % 2 == 0:
            kb.row()
        is_current = mode.name == current
        label = f"✅ {mode.title}" if is_current else mode.title
        color = KeyboardButtonColor.POSITIVE if is_current else KeyboardButtonColor.SECONDARY
        kb.add(_button(label, "mode", mode=mode.name), color=color)
    kb.row()
    kb.add(_button("◀️ Назад", "menu"), color=KeyboardButtonColor.PRIMARY)
    return kb.get_json()


def settings() -> str:
    kb = Keyboard(inline=True)
    kb.add(_button("🧹 Очистить историю диалога", "clear"), color=KeyboardButtonColor.NEGATIVE)
    kb.row()
    kb.add(_button("🏠 Меню", "menu"), color=KeyboardButtonColor.PRIMARY)
    return kb.get_json()
