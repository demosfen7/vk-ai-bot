"""Клиент OpenAI Chat Completions API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from bot.config import OPENAI_API_URL
from bot.memory import ChatMessage

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class LLMError(Exception):
    """LLM не смогла ответить: сеть, таймаут, ошибка API или странный ответ."""


def build_messages(
    system_prompt: str, history: list[ChatMessage], user_message: str
) -> list[dict[str, str]]:
    """Собирает контекст: system-сообщение, история диалога и новое сообщение пользователя."""
    return [
        {"role": "system", "content": system_prompt},
        *({"role": m["role"], "content": m["content"]} for m in history),
        {"role": "user", "content": user_message},
    ]


class LLMClient:
    def __init__(self, api_key: str, model: str, api_url: str = OPENAI_API_URL) -> None:
        self.model = model
        self.api_url = api_url
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def complete(
        self, system_prompt: str, history: list[ChatMessage], user_message: str
    ) -> str:
        """Отправляет диалог в модель и возвращает текст ответа.

        Raises:
            LLMError: при любой проблеме с запросом или ответом.
        """
        payload = {
            "model": self.model,
            "messages": build_messages(system_prompt, history, user_message),
        }
        try:
            response = await self._client.post(self.api_url, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError("LLM API не ответил вовремя") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Сетевая ошибка при запросе к LLM API: {exc!r}") from exc

        if response.status_code != 200:
            raise LLMError(
                f"LLM API вернул HTTP {response.status_code}: {_error_message(response)}"
            )

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("Неожиданный формат ответа LLM API") from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM API вернул пустой ответ")
        return content.strip()

    async def aclose(self) -> None:
        await self._client.aclose()


def _error_message(response: httpx.Response) -> str:
    """Достаёт текст ошибки из ответа API (без заголовков и ключа)."""
    try:
        data: Any = response.json()
        return str(data["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.text[:300]
