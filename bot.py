"""Telegram-бот: рассылка просроченных сделок в 11:00 МСК.

Роли (задаются в USERS_JSON):
- manager: получает свои просроченные сделки; кнопка «Мои просроченные».
- admin:   получает общий список по всем менеджерам + инлайн-кнопки по каждому
           менеджеру (подпись = setter); кнопка «Все просроченные».
"""
from __future__ import annotations

import logging
from datetime import time
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config, User, load_config
from ingest import start_ingest_server
from sheets import (
    SnapshotNotReady,
    build_admin_message,
    build_message,
    build_setter_section,
    fetch_dataframe,
    overdue_all,
    overdue_for_setter,
)
from storage import SubscriberStore

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mgcom_nb_bot")

BUTTON_MANAGER = "Мои просроченные"
BUTTON_ADMIN = "Все просроченные"
MGR_PREFIX = "mgr:"
MESSAGE_LIMIT = 4000


def _manager_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BUTTON_MANAGER]], resize_keyboard=True, is_persistent=True)


def _admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BUTTON_ADMIN]], resize_keyboard=True, is_persistent=True)


def _managers_inline(cfg: Config) -> InlineKeyboardMarkup | None:
    """Инлайн-кнопки по каждому менеджеру (подпись = setter, callback = индекс)."""
    rows = [
        [InlineKeyboardButton(u.setter, callback_data=f"{MGR_PREFIX}{i}")]
        for i, u in enumerate(cfg.users)
        if u.role == "manager"
    ]
    return InlineKeyboardMarkup(rows) if rows else None


def _user_by_username(cfg: Config, username: str | None) -> User | None:
    if not username:
        return None
    return cfg.users_by_login.get(username.lstrip("@").lower())


def _greeting_for(user: User, tg_first_name: str | None) -> str:
    return user.greeting or (tg_first_name or "").strip() or "Привет"


def _split_html(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Режет длинное сообщение по строкам, чтобы влезть в лимит Telegram."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        candidate = line if not cur else cur + "\n" + line
        if len(candidate) > limit and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


async def _send_html(bot, chat_id: int, text: str, reply_markup=None):
    """Отправляет HTML-сообщение, при необходимости разбивая на части.

    reply_markup прикрепляется только к последней части.
    """
    chunks = _split_html(text)
    for i, chunk in enumerate(chunks):
        await bot.send_message(
            chat_id,
            chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]

    tg_user = update.effective_user
    user = _user_by_username(cfg, tg_user.username if tg_user else None)

    if user is None:
        await update.message.reply_text("Доступ ограничен.")
        logger.info("Отказ в доступе: username=%s", tg_user.username if tg_user else None)
        return

    store.set(user.login, update.effective_chat.id)
    logger.info(
        "Подписан %s (role=%s, setter=%s, chat_id=%s)",
        user.login, user.role, user.setter, update.effective_chat.id,
    )

    when = f"{cfg.send_hour:02d}:{cfg.send_minute:02d}"
    if user.is_admin:
        name = user.greeting or "коллега"
        await update.message.reply_text(
            f"{name}, привет! 👋\n\n"
            "Я слежу за просроченными сделками по всем менеджерам.\n\n"
            f"Каждый день в {when} по Москве я пришлю общий список просроченных сделок, "
            "сгруппированный по менеджерам. Под сообщением будут кнопки по каждому "
            "менеджеру — нажми, чтобы получить только его сделки. А кнопка "
            "«Все просроченные» под полем ввода покажет общий список в любой момент.",
            reply_markup=_admin_keyboard(),
        )
    else:
        name = user.greeting or user.setter
        await update.message.reply_text(
            f"{name}, привет! 👋\n\n"
            "Я слежу за твоими просроченными сделками из таблицы и напоминаю о них.\n\n"
            f"Каждый день в {when} по Москве я буду присылать список сделок, где ты "
            "указан постановщиком, а дедлайн (ДЛ) уже прошёл. А кнопка «Мои просроченные» "
            "под полем ввода в любой момент покажет актуальные данные.\n\n"
            f"Я узнал тебя как постановщика: {user.setter}.",
            reply_markup=_manager_keyboard(),
        )


async def on_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрос сделок по команде /deals или по кнопке (роле-зависимо)."""
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]

    tg_user = update.effective_user
    user = _user_by_username(cfg, tg_user.username if tg_user else None)
    if user is None:
        await update.message.reply_text("Доступ ограничен.")
        return

    store.set(user.login, update.effective_chat.id)
    chat_id = update.effective_chat.id

    try:
        df = fetch_dataframe(cfg)
        if user.is_admin:
            text = build_admin_message(user.greeting or "Коллеги", overdue_all(df, cfg), cfg)
            await _send_html(context.bot, chat_id, text, reply_markup=_managers_inline(cfg))
        else:
            greeting = _greeting_for(user, tg_user.first_name if tg_user else None)
            text = build_message(greeting, overdue_for_setter(df, user.setter, cfg), cfg)
            await _send_html(context.bot, chat_id, text, reply_markup=_manager_keyboard())
    except SnapshotNotReady as exc:
        await update.message.reply_text(str(exc))
    except Exception:
        logger.exception("Ошибка запроса для %s", user.login)
        await update.message.reply_text("Не удалось загрузить данные из таблицы. Попробуйте позже.")


async def on_manager_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ нажал инлайн-кнопку менеджера — присылаем сделки этого менеджера."""
    cfg: Config = context.application.bot_data["cfg"]
    query = update.callback_query

    tg_user = update.effective_user
    user = _user_by_username(cfg, tg_user.username if tg_user else None)
    if user is None or not user.is_admin:
        await query.answer("Нет доступа", show_alert=True)
        return

    await query.answer()

    try:
        idx = int(query.data[len(MGR_PREFIX):])
        target = cfg.users[idx]
    except (ValueError, IndexError):
        await query.message.reply_text("Неизвестный менеджер.")
        return

    try:
        df = fetch_dataframe(cfg)
        deals = overdue_for_setter(df, target.setter, cfg)
    except SnapshotNotReady as exc:
        await query.message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Ошибка получения сделок менеджера %s", target.setter)
        await query.message.reply_text("Не удалось загрузить данные из таблицы. Попробуйте позже.")
        return

    text = build_setter_section(target.setter, deals, cfg)
    await _send_html(context.bot, query.message.chat_id, text)


async def send_daily_reports(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]

    try:
        df = fetch_dataframe(cfg)
    except SnapshotNotReady:
        logger.warning("Авторассылка пропущена: снапшот таблицы ещё не получен")
        return
    except Exception:
        logger.exception("Не удалось прочитать снапшот таблицы для авторассылки")
        return

    sent = 0
    for user in cfg.users:
        chat_id = store.get(user.login)
        if chat_id is None:
            logger.info("Пропуск %s: не нажимал /start", user.login)
            continue

        if user.is_admin:
            overdue = overdue_all(df, cfg)
            if overdue.empty and not cfg.send_when_empty:
                continue
            text = build_admin_message(user.greeting or "Коллеги", overdue, cfg)
            reply_markup = _managers_inline(cfg)
        else:
            deals = overdue_for_setter(df, user.setter, cfg)
            if deals.empty and not cfg.send_when_empty:
                continue
            text = build_message(user.greeting or "Коллега", deals, cfg)
            reply_markup = _manager_keyboard()

        try:
            await _send_html(context.bot, chat_id, text, reply_markup=reply_markup)
            sent += 1
        except Forbidden:
            logger.warning("Пользователь %s заблокировал бота", user.login)
        except TelegramError:
            logger.exception("Ошибка отправки пользователю %s", user.login)

    logger.info("Авторассылка завершена: отправлено %s сообщений", sent)


def main() -> None:
    cfg = load_config()
    store = SubscriberStore(cfg.data_dir)

    start_ingest_server(cfg)

    app = Application.builder().token(cfg.bot_token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["store"] = store

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("deals", on_request))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(f"^({BUTTON_MANAGER}|{BUTTON_ADMIN})$"),
            on_request,
        )
    )
    app.add_handler(CallbackQueryHandler(on_manager_button, pattern=rf"^{MGR_PREFIX}\d+$"))

    app.job_queue.run_daily(
        send_daily_reports,
        time=time(hour=cfg.send_hour, minute=cfg.send_minute, tzinfo=ZoneInfo(cfg.timezone)),
        name="daily_reports",
    )

    admins = sum(1 for u in cfg.users if u.is_admin)
    logger.info(
        "Бот запущен. Рассылка в %02d:%02d %s. Пользователей: %d (админов: %d)",
        cfg.send_hour, cfg.send_minute, cfg.timezone, len(cfg.users), admins,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
