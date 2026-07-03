"""Telegram-бот: рассылка просроченных сделок и сделок на принятие решения.

Роли (задаются в USERS_JSON / users.json):
- manager: свои просроченные сделки; кнопка «Мои просроченные».
- head/admin: 4 кнопки — «Мои просроченные», «Все просроченные»,
           «Мои решения», «Все решения». admin дополнительно получает
           инлайн-кнопки по каждому менеджеру (подпись = setter).
"""
from __future__ import annotations

import logging
from datetime import datetime, time
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
    build_decision_message,
    build_message,
    build_setter_section,
    decision_deals,
    fetch_dataframe,
    filter_setter,
    overdue_all,
    overdue_for_setter,
)
from storage import SubscriberStore

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mgcom_nb_bot")

BUTTON_MY_OVERDUE = "Мои просроченные"
BUTTON_ALL_OVERDUE = "Все просроченные"
BUTTON_MY_DECISION = "Мои решения"
BUTTON_ALL_DECISION = "Все решения"
MGR_PREFIX = "mgr:"
MESSAGE_LIMIT = 4000


def _manager_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BUTTON_MY_OVERDUE]], resize_keyboard=True, is_persistent=True)


def _head_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BUTTON_MY_OVERDUE, BUTTON_ALL_OVERDUE],
            [BUTTON_MY_DECISION, BUTTON_ALL_DECISION],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _keyboard_for(user: User) -> ReplyKeyboardMarkup:
    if user.is_admin or user.is_head:
        return _head_admin_keyboard()
    return _manager_keyboard()


def _times_str(cfg: Config) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in cfg.send_times)


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

    when = _times_str(cfg)
    if user.is_admin or user.is_head:
        name = user.greeting or user.setter or "коллега"
        await update.message.reply_text(
            f"{name}, привет! 👋\n\n"
            "Я слежу за просроченными сделками и сделками на принятие решения.\n\n"
            f"Каждый день в {when} по Москве я пришлю сводку. "
            "Кнопки под полем ввода:\n"
            "• «Мои просроченные» — просрочка, где ты постановщик;\n"
            "• «Все просроченные» — общий список по всем менеджерам;\n"
            "• «Мои решения» — сделки на принятие решения по тебе;\n"
            "• «Все решения» — все сделки на принятие решения.\n\n"
            f"Я узнал тебя как постановщика: {user.setter}.",
            reply_markup=_head_admin_keyboard(),
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


async def _reply_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User, *, mine: bool) -> None:
    """Просроченные сделки из Google-снапшота: mine=True — свои, иначе все (сгруппировано)."""
    cfg: Config = context.application.bot_data["cfg"]
    chat_id = update.effective_chat.id
    tg_user = update.effective_user

    try:
        df = fetch_dataframe(cfg)
    except SnapshotNotReady as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Ошибка чтения снапшота просрочки для %s", user.login)
        await update.message.reply_text("Не удалось загрузить данные из таблицы. Попробуйте позже.")
        return

    if mine:
        greeting = _greeting_for(user, tg_user.first_name if tg_user else None)
        text = build_message(greeting, overdue_for_setter(df, user.setter, cfg), cfg)
        await _send_html(context.bot, chat_id, text, reply_markup=_keyboard_for(user))
    else:
        name = user.greeting or user.setter or "Коллеги"
        inline = _managers_inline(cfg) if user.is_admin else None
        text = build_admin_message(name, overdue_all(df, cfg), cfg)
        await _send_html(context.bot, chat_id, text, reply_markup=inline)


async def _reply_decision(update: Update, context: ContextTypes.DEFAULT_TYPE, user: User, *, mine: bool) -> None:
    """Принятие решения из листа decision_sheet: mine=True — только свои, иначе все."""
    cfg: Config = context.application.bot_data["cfg"]
    chat_id = update.effective_chat.id
    tg_user = update.effective_user

    try:
        df = fetch_dataframe(cfg, sheet=cfg.decision_sheet)
    except SnapshotNotReady as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Ошибка «решения» для %s", user.login)
        await update.message.reply_text("Не удалось загрузить данные из таблицы. Попробуйте позже.")
        return

    deals = decision_deals(df, cfg)
    if mine:
        greeting = _greeting_for(user, tg_user.first_name if tg_user else None)
        if cfg.col_setter not in deals.columns:
            await update.message.reply_text(
                f"В листе «{cfg.decision_sheet}» нет колонки «{cfg.col_setter}» — "
                "не могу отфильтровать по постановщику. Доступны «Все решения»."
            )
            return
        deals = filter_setter(deals, user.setter, cfg)
        text = build_decision_message(greeting, deals, cfg)
    else:
        text = build_decision_message(user.greeting or user.setter or "Коллеги", deals, cfg)
    await _send_html(context.bot, chat_id, text, reply_markup=_keyboard_for(user))


async def on_my_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]
    tg_user = update.effective_user
    user = _user_by_username(cfg, tg_user.username if tg_user else None)
    if user is None:
        await update.message.reply_text("Доступ ограничен.")
        return
    store.set(user.login, update.effective_chat.id)
    await _reply_overdue(update, context, user, mine=True)


async def on_all_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]
    tg_user = update.effective_user
    user = _user_by_username(cfg, tg_user.username if tg_user else None)
    if user is None or not (user.is_head or user.is_admin):
        await update.message.reply_text("Доступ ограничен.")
        return
    store.set(user.login, update.effective_chat.id)
    await _reply_overdue(update, context, user, mine=False)


async def on_my_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]
    tg_user = update.effective_user
    user = _user_by_username(cfg, tg_user.username if tg_user else None)
    if user is None or not (user.is_head or user.is_admin):
        await update.message.reply_text("Доступ ограничен.")
        return
    store.set(user.login, update.effective_chat.id)
    await _reply_decision(update, context, user, mine=True)


async def on_all_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]
    tg_user = update.effective_user
    user = _user_by_username(cfg, tg_user.username if tg_user else None)
    if user is None or not (user.is_head or user.is_admin):
        await update.message.reply_text("Доступ ограничен.")
        return
    store.set(user.login, update.effective_chat.id)
    await _reply_decision(update, context, user, mine=False)


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

    chat_id = query.message.chat_id
    try:
        df = fetch_dataframe(cfg)
    except SnapshotNotReady as exc:
        await query.message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Ошибка получения сделок менеджера %s", target.setter)
        await query.message.reply_text("Не удалось загрузить данные из таблицы. Попробуйте позже.")
        return

    text = build_setter_section(target.setter, overdue_for_setter(df, target.setter, cfg), cfg)
    await _send_html(context.bot, chat_id, text)


async def send_daily_reports(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    store: SubscriberStore = context.application.bot_data["store"]

    # По выходным (суббота, воскресенье) авторассылку не делаем.
    weekday = datetime.now(ZoneInfo(cfg.timezone)).weekday()  # 0=Пн ... 5=Сб, 6=Вс
    if weekday >= 5:
        logger.info("Выходной день — авторассылка пропущена")
        return

    # Просроченные — из снапшота Google (лист «Просрок чистка»).
    df = None
    try:
        df = fetch_dataframe(cfg)
    except SnapshotNotReady:
        logger.warning("Снапшот просрочки ещё не получен — пропускаю просроченные сделки")
    except Exception:
        logger.exception("Авторассылка: не удалось прочитать снапшот просрочки")

    # Принятие решения — из снапшота Google (нужно только head/admin).
    df_decision = None
    try:
        df_decision = fetch_dataframe(cfg, sheet=cfg.decision_sheet)
    except SnapshotNotReady:
        logger.warning("Лист «%s» ещё не получен — пропускаю сообщения принятия решения", cfg.decision_sheet)
    except Exception:
        logger.exception("Не удалось прочитать лист принятия решения")

    if df is None and df_decision is None:
        logger.warning("Авторассылка пропущена: нет данных в снапшоте")
        return

    sent = 0
    for user in cfg.users:
        chat_id = store.get(user.login)
        if chat_id is None:
            logger.info("Пропуск %s: не нажимал /start", user.login)
            continue

        name = user.greeting or user.setter or "Коллеги"
        # Собираем сообщения для пользователя: (текст, разметка).
        messages: list[tuple[str, object]] = []

        # 1) Просроченные сделки (из снапшота Google).
        if df is not None:
            if user.is_admin:
                deals = overdue_all(df, cfg)
                if not (deals.empty and not cfg.send_when_empty):
                    messages.append((build_admin_message(name, deals, cfg), _managers_inline(cfg)))
            else:
                deals = overdue_for_setter(df, user.setter, cfg)
                if not (deals.empty and not cfg.send_when_empty):
                    messages.append((build_message(name, deals, cfg), _keyboard_for(user)))

        # 2) Принятие решения — только для head и admin.
        #    admin — все решения, head — только свои (по setter).
        if (user.is_head or user.is_admin) and df_decision is not None:
            ddeals = decision_deals(df_decision, cfg)
            if user.is_head and not user.is_admin:
                ddeals = filter_setter(ddeals, user.setter, cfg)
            if not (ddeals.empty and not cfg.send_when_empty):
                messages.append((build_decision_message(name, ddeals, cfg), _keyboard_for(user)))

        for text, reply_markup in messages:
            try:
                await _send_html(context.bot, chat_id, text, reply_markup=reply_markup)
                sent += 1
            except Forbidden:
                logger.warning("Пользователь %s заблокировал бота", user.login)
                break
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
    app.add_handler(CommandHandler("deals", on_my_overdue))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_MY_OVERDUE}$"), on_my_overdue)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_ALL_OVERDUE}$"), on_all_overdue)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_MY_DECISION}$"), on_my_decision)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_ALL_DECISION}$"), on_all_decision)
    )
    app.add_handler(CallbackQueryHandler(on_manager_button, pattern=rf"^{MGR_PREFIX}\d+$"))

    tz = ZoneInfo(cfg.timezone)
    for hour, minute in cfg.send_times:
        app.job_queue.run_daily(
            send_daily_reports,
            time=time(hour=hour, minute=minute, tzinfo=tz),
            name=f"daily_{hour:02d}{minute:02d}",
        )

    admins = sum(1 for u in cfg.users if u.is_admin)
    logger.info(
        "Бот запущен. Рассылка в %s %s. Пользователей: %d (админов: %d)",
        _times_str(cfg), cfg.timezone, len(cfg.users), admins,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
