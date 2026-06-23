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


def overdue_for_setter(df: pd.DataFrame, setter: str, cfg: Config) -> pd.DataFrame:
    """Возвращает просроченные сделки конкретного постановщика.

    Просроченная = "Дата окончания" строго раньше сегодняшней даты (по TZ из конфига).
    """
    _ensure_columns(df, cfg)

    mine = df[df[cfg.col_setter].str.strip().str.casefold() == setter.strip().casefold()]
    if mine.empty:
        return mine

    deadlines = pd.to_datetime(
        mine[cfg.col_deadline], errors="coerce", dayfirst=True
    )
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    overdue_mask = deadlines.dt.date < today
    # Строки с нераспознанной датой пропускаем (NaT -> mask False).
    return mine[overdue_mask.fillna(False)]


def build_message(greeting: str, deals: pd.DataFrame, cfg: Config) -> str:
    """Собирает HTML-сообщение со списком сделок-гиперссылок (как на образце)."""
    greet = html.escape(greeting) if greeting else "Привет"
    if deals.empty:
        return f"{greet}, привет!\nПросроченных сделок нет 👍"

    lines = []
    for _, row in deals.iterrows():
        name = html.escape(str(row[cfg.col_name]).strip()) or "(без названия)"
        url = str(row[cfg.col_link]).strip()
        if url:
            safe_url = html.escape(url, quote=True)
            lines.append(f'<a href="{safe_url}">{name}</a>')
        else:
            lines.append(name)

    header = f"{greet}, привет!\nСделки с просроченным ДЛ:"
    return header + "\n\n" + "\n".join(lines)
