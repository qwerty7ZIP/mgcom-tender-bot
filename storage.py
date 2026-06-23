"""Хранение chat_id подписавшихся пользователей (логин ТГ -> chat_id)."""
from __future__ import annotations

import json
import os
import threading

_lock = threading.Lock()


class SubscriberStore:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "subscribers.json")
        self._data: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {str(k).lower(): int(v) for k, v in raw.items()}
        except (json.JSONDecodeError, ValueError, OSError):
            return {}

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def set(self, login: str, chat_id: int) -> None:
        with _lock:
            self._data[login.lower()] = int(chat_id)
            self._save()

    def get(self, login: str) -> int | None:
        return self._data.get(login.lower())

    def all(self) -> dict[str, int]:
        return dict(self._data)
