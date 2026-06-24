"""Telegram-бот: рассылка просроченных сделок менеджерам в 11:00 МСК + кнопка «Мои просроченные»."""
from __future__ import annotations

import logging
from datetime import time
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config, User, load_config
from ingest import start_ingest_server
from sheets import SnapshotNotReady, build_message, fetch_dataframe, overdue_for_setter
from storage import SubscriberStore

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mgcom_nb_bot")

BUTTON_TEXT = "Мои просроченные"


def _keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_TEXT]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _user_by_username(cfg: Config, username: str | None) -> User | None:
    if not username:
        return None
    return cfg.users_by_login.get(username.lstrip("@").lower())


def _greeting_for(user: User, tg_first_name: str | None) -> str:
    return user.greeting or (tg_first_name or "").strip() or "Привет"


def _render_for_user(cfg: Config, user: User, greeting: str) -> str:
    df = fetch_dataframe(cfg)
    deals = overdue_for_setter(df, user.setter, cfg)
    return build_message(greeting, deals, cfg)


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
    logger.info("Подписан %s (setter=%s, chat_id=%s)", user.login, user.setter, update.effective_chat.id)

    name = user.greeting or user.setter
    await update.message.reply_text(
        f"{name}, привет! 👋\n\n"
        "Я слежу за твоими просроченными сделками из таблицы и напоминаю о них.\n\n"
        f"Каждый день в {cfg.send_hour:02d}:{cfg.send_minute:02d} по Москве я буду "
        "присылать список сделок, где ты указан постановщиком, а дедлайн (ДЛ) уже прошёл. "
        "А кнопка «Мои просроченные» под полем ввода в любой момент покажет "
        "актуальные данные.\n\n"
        f"Я узнал тебя как постановщика: {user.setter}.",
        reply_markup=_keyboard(),
    )


async def deals_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]

    tg_user = update.effective_user
    user = _user_by_username(cfg, tg_user.username if tg_user else None)
    if user is None:
        await update.message.reply_text("Доступ ограничен.")
        return

    store.set(user.login, update.effective_chat.id)
    greeting = _greeting_for(user, tg_user.first_name if tg_user else None)
    try:
        text = _render_for_user(cfg, user, greeting)
    except SnapshotNotReady as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Ошибка при загрузке таблицы для %s", user.login)
        await update.message.reply_text("Не удалось загрузить данные из таблицы. Попробуйте позже.")
        return

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_keyboard(),
    )


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

        deals = overdue_for_setter(df, user.setter, cfg)
        if deals.empty and not cfg.send_when_empty:
            continue

        text = build_message(user.greeting or "Коллега", deals, cfg)
        try:
            await context.bot.send_message(
                chat_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_keyboard(),
            )
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
    app.add_handler(CommandHandler("deals", deals_now))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_TEXT}$"), deals_now)
    )

    app.job_queue.run_daily(
        send_daily_reports,
        time=time(hour=cfg.send_hour, minute=cfg.send_minute, tzinfo=ZoneInfo(cfg.timezone)),
        name="daily_reports",
    )

    logger.info(
        "Бот запущен. Рассылка в %02d:%02d %s. Пользователей в whitelist: %d",
        cfg.send_hour, cfg.send_minute, cfg.timezone, len(cfg.users),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
