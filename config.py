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
    # Время(-ена) ежедневной рассылки по timezone, список (час, минута).
    send_times: tuple[tuple[int, int], ...] = ((11, 0), (17, 0))

    # Названия колонок в таблице (можно переопределить через env).
    col_id: str = "ID"
    col_name: str = "Название"
    col_setter: str = "Постановщик"
    col_deadline: str = "Дата окончания"
    col_link: str = "Ссылка"

    # Имена листов в Google-таблице (снапшот пушится через Apps Script).
    primary_sheet: str = "Просрок чистка"  # источник просроченных сделок
    decision_sheet: str = "Принятие чистка"  # источник для кнопок "Мои/Все решения"

    # Слать ли сообщение, если просроченных сделок нет (для авторассылки в 11:00).
    send_when_empty: bool = False

    # Куда складывать данные подписчиков (chat_id) и снапшот таблицы.
    data_dir: str = "data"

    users_by_login: dict[str, User] = field(default_factory=dict)

    # --- Прогноз повторных тендеров (лист со всеми сделками) ---
    tenders_sheet: str = "Импорт"
    # «Тот же тендер» = совпало название сделки (без регистра, знаков препинания и годов).
    tender_match_cols: tuple[str, ...] = ("Название сделки",)
    tender_col_invite: str = "Дата приглашения"
    # Запасные даты для проверки «тендер этого года уже заведён», если нет даты приглашения.
    tender_fallback_date_cols: tuple[str, ...] = ("Дата начала КП", "Дата окончания КП", "ДЛ")
    tender_col_name: str = "Название сделки"
    tender_col_link: str = "Ссылка"
    tender_col_setter: str = "Ответственный"
    tender_info_cols: tuple[str, ...] = (
        "Тип тендера", "Сумма тендера", "Статус тендера (Этап)", "Предмет тендера",
    )  # доп. инфо в сообщении
    tender_cycle_months: int = 12  # через сколько ждём повторный тендер
    tender_lead_months: int = 3  # за сколько предупреждать
    tender_grace_days: int = 30  # сколько дней показывать «ожидался, но записи нет»
    tender_button_spread_days: int = 7  # кнопка: сегодня + TENDER_LEAD_MONTHS ± N дней
    tender_fresh_days: int = 180  # запись позже прошлого тендера на N дней = тендер этого года
    tender_notify_time: tuple[int, int] | None = None  # None = первое время из SEND_TIMES

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


def _parse_times(raw: str) -> tuple[tuple[int, int], ...]:
    """SEND_TIMES — времена рассылки через запятую в формате HH:MM (напр. '11:00,17:00')."""
    times: list[tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(":")
        hour, minute = int(hh), int(mm or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise RuntimeError(f"SEND_TIMES: некорректное время '{part}'")
        times.append((hour, minute))
    return tuple(times) if times else ((11, 0),)


def _load_users() -> list[User]:
    """Список пользователей: из USERS_JSON (одна строка) или из файла USERS_FILE.

    Приоритет у USERS_JSON. Если он не задан — читаем JSON-файл (можно с отступами
    и переносами строк), путь берём из USERS_FILE (по умолчанию users.json).
    """
    raw = os.getenv("USERS_JSON", "").strip()
    if raw:
        return _parse_users(raw)

    path = os.getenv("USERS_FILE", "users.json").strip() or "users.json"
    if not os.path.exists(path):
        raise RuntimeError(
            "Список пользователей не задан: укажи USERS_JSON в .env "
            f"или создай файл {path} (см. users.json.example)."
        )
    with open(path, "r", encoding="utf-8") as f:
        return _parse_users(f.read())


def load_config() -> Config:
    users = _load_users()
    cfg = Config(
        bot_token=_require("BOT_TOKEN"),
        users=users,
        ingest_token=_require("INGEST_TOKEN"),
        ingest_host=os.getenv("INGEST_HOST", "0.0.0.0").strip() or "0.0.0.0",
        ingest_port=_int("INGEST_PORT", 8080),
        timezone=os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow",
        send_times=_parse_times(os.getenv("SEND_TIMES", "11:00,17:00")),
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
        **_tender_settings(),
    )
    return cfg


# --- Прогноз повторных тендеров ---

def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _str(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def _tender_settings() -> dict:
    """Настройки прогноза тендеров (по умолчанию — под лист «Импорт»)."""
    notify_raw = os.getenv("TENDER_NOTIFY_TIME", "").strip()
    return dict(
        tenders_sheet=_str("TENDERS_SHEET", Config.tenders_sheet),
        tender_match_cols=_list("TENDER_MATCH_COLS", Config.tender_match_cols)
        or Config.tender_match_cols,
        tender_col_invite=_str("TENDER_COL_INVITE", Config.tender_col_invite),
        tender_fallback_date_cols=_list("TENDER_FALLBACK_DATE_COLS", Config.tender_fallback_date_cols),
        tender_col_name=_str("TENDER_COL_NAME", Config.tender_col_name),
        tender_col_link=_str("TENDER_COL_LINK", Config.tender_col_link),
        tender_col_setter=_str("TENDER_COL_SETTER", Config.tender_col_setter),
        tender_info_cols=_list("TENDER_INFO_COLS", Config.tender_info_cols),
        tender_cycle_months=_int("TENDER_CYCLE_MONTHS", 12),
        tender_lead_months=_int("TENDER_LEAD_MONTHS", 3),
        tender_grace_days=_int("TENDER_GRACE_DAYS", 30),
        tender_button_spread_days=_int("TENDER_BUTTON_SPREAD_DAYS", 7),
        tender_fresh_days=_int("TENDER_FRESH_DAYS", 180),
        tender_notify_time=_parse_times(notify_raw)[0] if notify_raw else None,
    )
