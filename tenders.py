"""Прогноз повторных тендеров по листу со всеми сделками (по умолчанию «Импорт»).

Логика:
- Тендеры на рекламу обычно проводятся раз в год.
- Записи одного тендера ищем по колонке «Название сделки» (TENDER_MATCH_COLS):
  сравнение без регистра, знаков препинания и годов (2025, 2026 …).
- Берём последний тендер клиента с заполненной «Датой приглашения».
  Ожидаемая дата следующего = дата приглашения + TENDER_CYCLE_MONTHS (12).
- Тендер попадает в «ближайшие», если ожидаемая дата в окне
  [сегодня − TENDER_GRACE_DAYS; сегодня + TENDER_LEAD_MONTHS (3 мес.)].
- Если у клиента уже есть более свежая запись (дата приглашения или, если её
  нет, одна из запасных дат TENDER_FALLBACK_DATE_COLS позже прошлого тендера
  на TENDER_FRESH_DAYS) — тендер этого года уже заведён, напоминать не нужно.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from config import Config
from sheets import _norm

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpcomingTender:
    key: str  # нормализованный ключ клиента (для дедупликации уведомлений)
    name: str  # название прошлогодней сделки
    link: str
    setter: str
    last_invite: date  # дата приглашения прошлого тендера
    expected: date  # ожидаемая дата следующего тендера
    info: tuple[tuple[str, str], ...]  # доп. колонки (название, значение)

    @property
    def notify_id(self) -> str:
        """Уникальный id «прогноза» — чтобы уведомлять о нём один раз."""
        return f"{self.key}|{self.last_invite.isoformat()}"


_URL_PREFIX = re.compile(r"^(https?://)?(www\.)?")
_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_NON_WORD = re.compile(r"[^\w]+")


def _match_value(col: str, value: object) -> str:
    """Значение колонки для сравнения записей одного клиента."""
    text = _norm(value)
    if not text or text in {"nan", "none", "-", "—"}:
        return ""
    if "сайт" in col.lower() or text.startswith(("http://", "https://", "www.")):
        text = _URL_PREFIX.sub("", text).split("/")[0].strip()
        return text
    text = _YEAR.sub(" ", text)
    text = re.sub(r"\bг\b", " ", _NON_WORD.sub(" ", text))
    return " ".join(text.replace("_", " ").split())


def _parse_dates(series: pd.Series) -> pd.Series:
    """Даты из таблицы (как их видно в ячейках): 15.01.2026, 15.01.2026 12:00 и т.п."""
    cleaned = series.astype(str).str.strip().replace({"": None, "nan": None, "None": None})
    return pd.to_datetime(cleaned, errors="coerce", dayfirst=True, format="mixed")


def _column(df: pd.DataFrame, col: str) -> pd.Series:
    """Колонка по имени (если имя повторяется — первая)."""
    data = df.loc[:, df.columns == col]
    return data.iloc[:, 0]


def _ensure_tender_columns(df: pd.DataFrame, cfg: Config) -> None:
    missing = [c for c in (cfg.tender_col_invite,) if c not in df.columns]
    if not any(c in df.columns for c in cfg.tender_match_cols):
        missing += list(cfg.tender_match_cols)
    if missing:
        raise RuntimeError(
            f"В листе «{cfg.tenders_sheet}» нет колонок: "
            + ", ".join(missing)
            + f". Доступные колонки: {[c for c in df.columns if c]}"
        )


def _cell(df: pd.DataFrame, idx, col: str) -> str:
    if not col or col not in df.columns:
        return ""
    value = str(_column(df, col).loc[idx]).strip()
    return "" if value.lower() in {"nan", "none", "nat"} else value


def _group_ids(df: pd.DataFrame, cfg: Config) -> pd.Series:
    """Номер группы «один клиент» для каждой строки (совпадение по любой из колонок)."""
    parent = {idx: idx for idx in df.index}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for col in cfg.tender_match_cols:
        if col not in df.columns:
            continue
        first_seen: dict[str, object] = {}
        for idx, value in _column(df, col).items():
            key = _match_value(col, value)
            if not key:
                continue
            if key in first_seen:
                parent[find(idx)] = find(first_seen[key])
            else:
                first_seen[key] = idx
    return pd.Series({idx: find(idx) for idx in df.index})


def _client_key(df: pd.DataFrame, idx, cfg: Config) -> str:
    for col in cfg.tender_match_cols:
        if col in df.columns:
            key = _match_value(col, _column(df, col).loc[idx])
            if key:
                return key
    return str(idx)


def upcoming_tenders(df: pd.DataFrame, cfg: Config, today: date | None = None) -> list[UpcomingTender]:
    """Тендеры, которые ожидаются в ближайшие TENDER_LEAD_MONTHS и ещё не заведены."""
    _ensure_tender_columns(df, cfg)
    if today is None:
        today = datetime.now(ZoneInfo(cfg.timezone)).date()

    df = df.reset_index(drop=True)
    invite = _parse_dates(_column(df, cfg.tender_col_invite))
    # «Свежесть» записи: дата приглашения, а если её нет — первая заполненная запасная дата.
    any_date = invite.copy()
    for col in cfg.tender_fallback_date_cols:
        if col in df.columns:
            any_date = any_date.fillna(_parse_dates(_column(df, col)))
    groups = _group_ids(df, cfg)

    window_start = today - timedelta(days=cfg.tender_grace_days)
    window_end = (pd.Timestamp(today) + pd.DateOffset(months=cfg.tender_lead_months)).date()
    cycle = pd.DateOffset(months=cfg.tender_cycle_months)
    fresh_gap = pd.Timedelta(days=cfg.tender_fresh_days)

    result: list[UpcomingTender] = []
    for _, members in groups.groupby(groups):
        idxs = members.index
        group_invites = invite.loc[idxs].dropna()
        if group_invites.empty:
            continue
        base_idx = group_invites.idxmax()
        last_invite: pd.Timestamp = group_invites.loc[base_idx]
        expected = (last_invite + cycle).date()
        if not (window_start <= expected <= window_end):
            continue

        # Уже есть запись о тендере этого года? Тогда напоминать не нужно.
        if (any_date.loc[idxs] > last_invite + fresh_gap).any():
            continue

        info = tuple(
            (col, _cell(df, base_idx, col))
            for col in cfg.tender_info_cols
            if _cell(df, base_idx, col)
        )
        result.append(
            UpcomingTender(
                key=_client_key(df, base_idx, cfg),
                name=_cell(df, base_idx, cfg.tender_col_name),
                link=_cell(df, base_idx, cfg.tender_col_link),
                setter=_cell(df, base_idx, cfg.tender_col_setter),
                last_invite=last_invite.date(),
                expected=expected,
                info=info,
            )
        )

    result.sort(key=lambda t: (t.expected, t.name.lower()))
    return result


def tenders_for_setter(tenders: list[UpcomingTender], setter: str) -> list[UpcomingTender]:
    target = _norm(setter)
    return [t for t in tenders if _norm(t.setter) == target]


# --------------------------------------------------------------------------
# Форматирование сообщений
# --------------------------------------------------------------------------

def _fmt(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _when(expected: date, today: date) -> str:
    days = (expected - today).days
    if days < 0:
        return f"ожидался {-days} дн. назад, записи пока нет"
    if days == 0:
        return "ожидается сегодня"
    return f"через {days} дн."


def _tender_block(t: UpcomingTender, today: date, *, show_setter: bool) -> str:
    name = f"<b>{html.escape(t.name) or '(без названия)'}</b>"
    if t.link:
        name = f'<a href="{html.escape(t.link, quote=True)}">{name}</a>'
    lines = [
        f"{name} — ~{_fmt(t.expected)} ({_when(t.expected, today)})",
        f"Прошлое приглашение: {_fmt(t.last_invite)}",
    ]

    for col, value in t.info:
        lines.append(f"{html.escape(col)}: {html.escape(value)}")
    if show_setter and t.setter:
        lines.append(f"Ответственный: {html.escape(t.setter)}")
    return "\n".join(lines)


def build_upcoming_message(
    greeting: str, tenders: list[UpcomingTender], cfg: Config, *, show_setter: bool, today: date | None = None
) -> str:
    """Ответ на кнопку «Ближайшие тендеры»."""
    if today is None:
        today = datetime.now(ZoneInfo(cfg.timezone)).date()
    greet = html.escape(greeting) if greeting else "Привет"
    months = cfg.tender_lead_months
    if not tenders:
        return f"{greet}, привет!\nНа ближайшие {months} мес. новых тендеров не ожидается 👍"
    blocks = [_tender_block(t, today, show_setter=show_setter) for t in tenders]
    header = (
        f"{greet}, привет!\nОжидаемые тендеры на ближайшие {months} мес. "
        f"(записи о них ещё нет), всего {len(tenders)}:"
    )
    return header + "\n\n" + "\n\n".join(blocks)


def build_reminder_message(
    tenders: list[UpcomingTender], cfg: Config, *, show_setter: bool, today: date | None = None
) -> str:
    """Авто-уведомление о новых тендерах, которые попали в окно 3 месяцев."""
    if today is None:
        today = datetime.now(ZoneInfo(cfg.timezone)).date()
    blocks = [_tender_block(t, today, show_setter=show_setter) for t in tenders]
    head = (
        "🔔 Скоро повторный тендер!\n"
        f"Примерно через {cfg.tender_lead_months} мес. у клиента ожидается тендер "
        "(прошлый был год назад, записи о новом пока нет):"
        if len(tenders) == 1
        else "🔔 Скоро повторные тендеры!\n"
        f"Примерно в ближайшие {cfg.tender_lead_months} мес. ожидаются тендеры "
        "(прошлые были год назад, записей о новых пока нет):"
    )
    return head + "\n\n" + "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Какие уведомления уже отправлены
# --------------------------------------------------------------------------

class TenderNotifyStore:
    """Хранит id прогнозов, о которых уже уведомили (data/tender_notified.json)."""

    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "tender_notified.json")
        self._lock = threading.Lock()
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {str(k): str(v) for k, v in raw.items()}
        except (json.JSONDecodeError, OSError, AttributeError):
            return {}

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def is_notified(self, notify_id: str) -> bool:
        return notify_id in self._data

    def mark(self, notify_ids: list[str], today: date) -> None:
        with self._lock:
            for nid in notify_ids:
                self._data[nid] = today.isoformat()
            # Чистим записи старше 2 лет, чтобы файл не рос бесконечно.
            cutoff = (today - timedelta(days=730)).isoformat()
            self._data = {k: v for k, v in self._data.items() if v >= cutoff}
            self._save()
