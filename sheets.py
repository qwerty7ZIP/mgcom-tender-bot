"""Чтение снапшота таблицы (его присылает Apps Script) и формирование сообщений."""
from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from config import Config

logger = logging.getLogger(__name__)


class SnapshotNotReady(RuntimeError):
    """Снапшот ещё ни разу не получен от Apps Script."""


def fetch_dataframe(cfg: Config) -> pd.DataFrame:
    """Читает последний снапшот таблицы из локального файла и возвращает DataFrame.

    Снапшот присылает Apps Script (push) на HTTP-эндпоинт бота — см. ingest.py.
    Формат файла: {"updated_at": ..., "header": [...], "rows": [[...], ...]}.
    """
    path = cfg.snapshot_path
    if not os.path.exists(path):
        raise SnapshotNotReady(
            "Данные из таблицы ещё не получены. Apps Script пришлёт их в ближайшие минуты."
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    header = [str(c).strip() for c in data.get("header", [])]
    rows = data.get("rows", [])
    df = pd.DataFrame(rows, columns=header if header else None).astype(str)
    df = df.fillna("")
    return df


def _ensure_columns(df: pd.DataFrame, cfg: Config) -> None:
    required = [cfg.col_name, cfg.col_setter, cfg.col_deadline, cfg.col_link]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(
            "В таблице нет ожидаемых колонок: "
            + ", ".join(missing)
            + f". Доступные колонки: {list(df.columns)}"
        )


def _overdue_mask(df: pd.DataFrame, cfg: Config) -> pd.Series:
    """Маска просроченных строк: "Дата окончания" строго раньше сегодня (по TZ)."""
    deadlines = pd.to_datetime(df[cfg.col_deadline], errors="coerce", dayfirst=True)
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    # Строки с нераспознанной датой пропускаем (NaT -> False).
    return (deadlines.dt.date < today).fillna(False)


def overdue_for_setter(df: pd.DataFrame, setter: str, cfg: Config) -> pd.DataFrame:
    """Просроченные сделки конкретного постановщика."""
    _ensure_columns(df, cfg)
    by_setter = df[cfg.col_setter].str.strip().str.casefold() == setter.strip().casefold()
    return df[by_setter & _overdue_mask(df, cfg)]


def overdue_all(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Все просроченные сделки (по всем постановщикам)."""
    _ensure_columns(df, cfg)
    return df[_overdue_mask(df, cfg)]


def _deal_line(row: pd.Series, cfg: Config) -> str:
    name = html.escape(str(row[cfg.col_name]).strip()) or "(без названия)"
    url = str(row[cfg.col_link]).strip()
    if url:
        return f'<a href="{html.escape(url, quote=True)}">{name}</a>'
    return name


def build_message(greeting: str, deals: pd.DataFrame, cfg: Config) -> str:
    """HTML-сообщение со списком сделок-гиперссылок (для менеджера, как на образце)."""
    greet = html.escape(greeting) if greeting else "Привет"
    if deals.empty:
        return f"{greet}, привет!\nПросроченных сделок нет 👍"

    lines = [_deal_line(row, cfg) for _, row in deals.iterrows()]
    return f"{greet}, привет!\nСделки с просроченным ДЛ:\n\n" + "\n".join(lines)


def build_setter_section(setter: str, deals: pd.DataFrame, cfg: Config) -> str:
    """HTML-сообщение по одному постановщику (для админа по кнопке менеджера)."""
    title = html.escape(setter.strip()) or "(без постановщика)"
    if deals.empty:
        return f"Просроченные сделки — {title}:\nНет 👍"
    lines = [_deal_line(row, cfg) for _, row in deals.iterrows()]
    return f"Просроченные сделки — {title}:\n\n" + "\n".join(lines)


def build_admin_message(greeting: str, overdue: pd.DataFrame, cfg: Config) -> str:
    """Общий список просроченных сделок, сгруппированный по постановщикам (для админа)."""
    greet = html.escape(greeting) if greeting else "Коллеги"
    if overdue.empty:
        return f"{greet}, привет!\nПросроченных сделок нет ни у кого 👍"

    setters = overdue[cfg.col_setter].astype(str).str.strip()
    parts = [f"{greet}, привет!", "Все просроченные сделки по менеджерам:", ""]

    for setter in sorted({s for s in setters if s}):
        group = overdue[setters == setter]
        parts.append(f"<b>{html.escape(setter)}</b>")
        parts.extend(_deal_line(row, cfg) for _, row in group.iterrows())
        parts.append("")

    empties = overdue[setters == ""]
    if not empties.empty:
        parts.append("<b>(без постановщика)</b>")
        parts.extend(_deal_line(row, cfg) for _, row in empties.iterrows())

    return "\n".join(parts).strip()
