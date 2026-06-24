"""Загрузка и валидация конфигурации из переменных окружения."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class User:
    """Доверенный пользователь: маппинг логин ТГ -> постановщик + роль."""

    login: str  # username в Telegram, без @, в нижнем регистре
    setter: str  # значение в колонке "Постановщик" (обязателен для manager/head)
    greeting: str = ""  # как обращаться; если пусто — берётся имя из Telegram
    role: str = "manager"  # "manager", "admin" или "head"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_head(self) -> bool:
        return self.role == "head"


@dataclass(frozen=True)
class Config:
    bot_token: str
    users: list[User]

    # Секрет для приёма снапшотов от Apps Script (push на /ingest).
    ingest_token: str
    ingest_host: str = "0.0.0.0"
    ingest_port: int = 8080

    timezone: str = "Europe/Moscow"
    send_hour: int = 11
    send_minute: int = 0

    # Названия колонок в таблице (можно переопределить через env).
    col_id: str = "ID"
    col_name: str = "Название"
    col_setter: str = "Постановщик"
    col_deadline: str = "Дата окончания"
    col_link: str = "Ссылка"

    # Имена листов в Google-таблице (должны совпадать с теми, что шлёт Apps Script).
    primary_sheet: str = "Просрок чистка"  # источник просроченных сделок
    decision_sheet: str = "Принятие чистка"  # источник для кнопки "Принятие решения"

    # Слать ли сообщение, если просроченных сделок нет (для авторассылки в 11:00).
    send_when_empty: bool = False

    # Куда складывать данные подписчиков (chat_id) и снапшот таблицы.
    data_dir: str = "data"

    users_by_login: dict[str, User] = field(default_factory=dict)

    @property
    def snapshot_path(self) -> str:
        return os.path.join(self.data_dir, "snapshot.json")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


_ROLE_ALIASES = {
    "manager": "manager",
    "менеджер": "manager",
    "admin": "admin",
    "админ": "admin",
    "head": "head",
    "руководитель": "head",
}


def _parse_users(raw: str) -> list[User]:
    """USERS_JSON — JSON-массив объектов {login, setter, greeting?, role?}."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"USERS_JSON содержит невалидный JSON: {exc}") from exc

    if not isinstance(data, list):
        raise RuntimeError("USERS_JSON должен быть JSON-массивом объектов")

    users: list[User] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise RuntimeError(f"USERS_JSON[{i}] должен быть объектом")
        login = str(item.get("login", "")).strip().lstrip("@").lower()
        setter = str(item.get("setter", "")).strip()
        greeting = str(item.get("greeting", "")).strip()
        role_raw = (str(item.get("role", "manager")).strip().lower() or "manager")
        role = _ROLE_ALIASES.get(role_raw)
        if role is None:
            raise RuntimeError(
                f"USERS_JSON[{i}]: 'role' должно быть 'manager', 'admin' или 'head'"
            )
        if not login:
            raise RuntimeError(f"USERS_JSON[{i}]: поле 'login' обязательно")
        if role in {"manager", "head"} and not setter:
            raise RuntimeError(
                f"USERS_JSON[{i}]: поле 'setter' обязательно для роли '{role}'"
            )
        users.append(User(login=login, setter=setter, greeting=greeting, role=role))
    if not users:
        raise RuntimeError("USERS_JSON не содержит ни одного пользователя")
    return users


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def load_config() -> Config:
    users = _parse_users(_require("USERS_JSON"))
    cfg = Config(
        bot_token=_require("BOT_TOKEN"),
        users=users,
        ingest_token=_require("INGEST_TOKEN"),
        ingest_host=os.getenv("INGEST_HOST", "0.0.0.0").strip() or "0.0.0.0",
        ingest_port=_int("INGEST_PORT", 8080),
        timezone=os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow",
        send_hour=_int("SEND_HOUR", 11),
        send_minute=_int("SEND_MINUTE", 0),
        col_id=os.getenv("COL_ID", "ID").strip() or "ID",
        col_name=os.getenv("COL_NAME", "Название").strip() or "Название",
        col_setter=os.getenv("COL_SETTER", "Постановщик").strip() or "Постановщик",
        col_deadline=os.getenv("COL_DEADLINE", "Дата окончания").strip()
        or "Дата окончания",
        col_link=os.getenv("COL_LINK", "Ссылка").strip() or "Ссылка",
        primary_sheet=os.getenv("PRIMARY_SHEET", "Просрок чистка").strip() or "Просрок чистка",
        decision_sheet=os.getenv("DECISION_SHEET", "Принятие чистка").strip() or "Принятие чистка",
        send_when_empty=_bool("SEND_WHEN_EMPTY", False),
        data_dir=os.getenv("DATA_DIR", "data").strip() or "data",
        users_by_login={u.login: u for u in users},
    )
    return cfg
