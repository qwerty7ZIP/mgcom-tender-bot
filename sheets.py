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


def _df_from_block(header: list, rows: list) -> pd.DataFrame:
    header = [str(c).strip() for c in header]
    df = pd.DataFrame(rows, columns=header if header else None).astype(str)
    return df.fillna("")


def fetch_dataframe(cfg: Config, sheet: str | None = None) -> pd.DataFrame:
    """Читает лист из локального снапшота и возвращает DataFrame (всё как строки).

    Снапшот присылает Apps Script (push) на HTTP-эндпоинт бота — см. ingest.py.
    Новый формат: {"updated_at": ..., "sheets": {"<имя>": {"header":[...], "rows":[...]}}}.
    Поддерживается и старый формат {"header":[...], "rows":[...]} (только основной лист).
    sheet=None — основной лист (cfg.primary_sheet).
    """
    path = cfg.snapshot_path
    if not os.path.exists(path):
        raise SnapshotNotReady(
            "Данные из таблицы ещё не получены. Apps Script пришлёт их в ближайшие минуты."
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    name = sheet or cfg.primary_sheet
    sheets = data.get("sheets")
    if isinstance(sheets, dict):
        block = sheets.get(name)
        if block is None:
            raise SnapshotNotReady(
                f"Лист «{name}» ещё не получен от Apps Script. "
                "Проверь, что он указан в списке листов в Code.gs."
            )
        return _df_from_block(block.get("header", []), block.get("rows", []))

    # Старый одно-листовый формат — доступен только основной лист.
    if sheet and sheet != cfg.primary_sheet:
        raise SnapshotNotReady(f"Лист «{name}» ещё не получен от Apps Script.")
    return _df_from_block(data.get("header", []), data.get("rows", []))


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


def filter_setter(df: pd.DataFrame, setter: str, cfg: Config) -> pd.DataFrame:
    """Строки конкретного постановщика без фильтра по дате.

    Используется для листа «Принятие решения» — там дата не важна.
    """
    return df[
        df[cfg.col_setter].astype(str).str.strip().str.casefold() == setter.strip().casefold()
    ]


def _deal_line(row: pd.Series, cfg: Config) -> str:
    name = html.escape(str(row[cfg.col_name]).strip()) or "(без названия)"
    url = str(row[cfg.col_link]).strip()
    if url:
        return f'<a href="{html.escape(url, quote=True)}">{name}</a>'
    return name


def decision_deals(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Все сделки из листа «Принятие чистка» (нужны только Название и Ссылка)."""
    missing = [c for c in (cfg.col_name, cfg.col_link) if c not in df.columns]
    if missing:
        raise RuntimeError(
            "В листе «Принятие чистка» нет колонок: "
            + ", ".join(missing)
            + f". Доступные колонки: {list(df.columns)}"
        )
    return df[df[cfg.col_name].astype(str).str.strip() != ""]


def _build_list(greeting: str, deals: pd.DataFrame, cfg: Config, *, title: str, empty: str) -> str:
    greet = html.escape(greeting) if greeting else "Привет"
    if deals.empty:
        return f"{greet}, привет!\n{empty}"
    lines = [_deal_line(row, cfg) for _, row in deals.iterrows()]
    return f"{greet}, привет!\n{title}\n\n" + "\n".join(lines)


def build_message(greeting: str, deals: pd.DataFrame, cfg: Config) -> str:
    """HTML-сообщение со списком просроченных сделок (для менеджера, как на образце)."""
    return _build_list(
        greeting, deals, cfg,
        title="Сделки с просроченным ДЛ:",
        empty="Просроченных сделок нет 👍",
    )


def build_decision_message(greeting: str, deals: pd.DataFrame, cfg: Config) -> str:
    """HTML-сообщение со списком сделок на принятие решения (для руководителя)."""
    return _build_list(
        greeting, deals, cfg,
        title="Сделки на принятие решения:",
        empty="Сделок на принятие решения нет 👍",
    )


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
