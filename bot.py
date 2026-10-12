"""Простой Telegram-бот для личных задач на python-telegram-bot."""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
LOGGER = logging.getLogger(__name__)

PRIORITIES = {"low": "Низкий", "normal": "Обычный", "high": "Высокий"}
PRIORITY_ICONS = {"low": "🟢", "normal": "🔵", "high": "🔴"}
REMINDERS = {0: "Без напоминания", 10: "За 10 минут", 60: "За 1 час", 1440: "За 1 день"}
DATETIME_FORMAT = "%d.%m.%Y %H:%M"
PAGE_SIZE = 10
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Open-Meteo возвращает числовые коды WMO, поэтому переводим их в короткий текст.
WEATHER_CODES = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность",
    3: "пасмурно", 45: "туман", 48: "изморозь и туман",
    51: "слабая морось", 53: "морось", 55: "сильная морось",
    56: "слабая ледяная морось", 57: "ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "слабый ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежные зёрна",
    80: "небольшой ливень", 81: "ливень", 82: "сильный ливень",
    85: "небольшой снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 97: "сильная гроза", 99: "сильная гроза с градом",
}
MENU_COMMANDS = [
    BotCommand("start", "Главное меню"),
    BotCommand("help", "Список команд и помощь"),
    BotCommand("add", "Добавить задачу"),
    BotCommand("tasks", "Все открытые задачи"),
    BotCommand("today", "Задачи на сегодня"),
    BotCommand("week", "Задачи за последние 7 дней"),
    BotCommand("search", "Поиск задач"),
    BotCommand("history", "История действий с задачами"),
    BotCommand("city", "Выбрать город для погоды"),
    BotCommand("weather", "Погода и прогноз на сегодня"),
    BotCommand("done", "Отметить задачу выполненной"),
    BotCommand("edit", "Изменить задачу"),
    BotCommand("delete", "Удалить задачу"),
    BotCommand("stats", "Статистика"),
    BotCommand("cancel", "Отменить ввод"),
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_db(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def local_tz(context: ContextTypes.DEFAULT_TYPE) -> ZoneInfo:
    return context.application.bot_data["timezone"]


def format_due(value: str | None, tz: ZoneInfo) -> str:
    due = from_db(value)
    return due.astimezone(tz).strftime(DATETIME_FORMAT) if due else "без срока"


@contextmanager
def database(path: Path):
    """Отдельное короткое соединение для каждой операции; commit при успехе."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def db_path(context: ContextTypes.DEFAULT_TYPE) -> Path:
    return context.application.bot_data["db_path"]


def renumber_active_tasks(db: sqlite3.Connection, user_id: int) -> None:
    """Нумерует только открытые задачи по порядку создания: 1, 2, 3..."""
    rows = db.execute(
        "SELECT id FROM tasks WHERE user_id = ? AND status = 'active' ORDER BY id",
        (user_id,),
    ).fetchall()
    # Сначала освобождаем номера, иначе уникальный индекс помешает их сдвигу.
    db.execute(
        "UPDATE tasks SET task_number = NULL WHERE user_id = ? AND status = 'active'",
        (user_id,),
    )
    db.executemany(
        "UPDATE tasks SET task_number = ? WHERE id = ?",
        ((number, row["id"]) for number, row in enumerate(rows, start=1)),
    )


def init_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                task_number INTEGER,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                priority TEXT NOT NULL CHECK(priority IN ('low', 'normal', 'high')),
                due_at TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'done')),
                created_at TEXT NOT NULL,
                completed_at TEXT,
                reminder_minutes INTEGER NOT NULL DEFAULT 0,
                reminder_sent_at TEXT
            )"""
        )
        # При обновлении старой базы выполненные задачи больше не занимают
        # номера. Все открытые задачи получают последовательные номера.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        if "task_number" not in columns:
            db.execute("ALTER TABLE tasks ADD COLUMN task_number INTEGER")
        db.execute("UPDATE tasks SET task_number = NULL WHERE status = 'done'")
        user_ids = db.execute(
            "SELECT DISTINCT user_id FROM tasks WHERE status = 'active'"
        ).fetchall()
        for row in user_ids:
            renumber_active_tasks(db, row["user_id"])
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_user_number "
            "ON tasks(user_id, task_number)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_reminder ON tasks(status, reminder_sent_at, due_at)")
        # Погода хранится отдельно: одна выбранная точка для каждого пользователя.
        db.execute(
            """CREATE TABLE IF NOT EXISTS user_locations (
                user_id INTEGER PRIMARY KEY,
                city_name TEXT NOT NULL,
                admin1 TEXT NOT NULL DEFAULT '',
                country TEXT,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL
            )"""
        )
        # Старые базы содержат координаты, но ещё не содержат регион города.
        location_columns = {row["name"] for row in db.execute("PRAGMA table_info(user_locations)")}
        if "admin1" not in location_columns:
            db.execute("ALTER TABLE user_locations ADD COLUMN admin1 TEXT NOT NULL DEFAULT ''")
        db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )"""
        )
        # Старые задачи остаются на месте; известных по ним пользователей переносим.
        db.execute(
            """INSERT OR IGNORE INTO users (user_id, chat_id, first_seen_at, last_seen_at)
               SELECT user_id, MAX(chat_id), MIN(created_at), MAX(created_at)
               FROM tasks GROUP BY user_id"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )"""
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_interactions_user_date "
            "ON interactions(user_id, occurred_at)"
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                task_number INTEGER,
                task_title TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('created', 'edited', 'completed', 'deleted')),
                field_name TEXT,
                old_value TEXT,
                new_value TEXT,
                occurred_at TEXT NOT NULL
            )"""
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_history_user_date "
            "ON task_history(user_id, occurred_at DESC, id DESC)"
        )


def record_task_action(
    db: sqlite3.Connection, user_id: int, task_id: int, number: int | None,
    title: str, action: str, *, field_name: str | None = None,
    old_value: str | None = None, new_value: str | None = None,
) -> None:
    """Пишет снимок действия в той же транзакции, что и изменение задачи."""
    db.execute(
        """INSERT INTO task_history
           (user_id, task_id, task_number, task_title, action,
            field_name, old_value, new_value, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, task_id, number, title, action, field_name,
         old_value, new_value, to_db(now_utc())),
    )


async def track_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Хранит профиль и тип обращения; текст пользовательских сообщений не сохраняет."""
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or chat.type != ChatType.PRIVATE:
        return
    if update.callback_query:
        kind = "button"
        name = str(update.callback_query.data or "").split(":", 1)[0][:50]
    elif update.message and update.message.text and update.message.text.startswith("/"):
        kind = "command"
        name = update.message.text.split(maxsplit=1)[0].split("@", 1)[0][:50]
    else:
        kind, name = "message", "text" if update.message and update.message.text else "other"
    timestamp = to_db(now_utc())
    with database(db_path(context)) as db:
        db.execute(
            """INSERT INTO users
               (user_id, chat_id, username, first_name, last_name, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               chat_id = excluded.chat_id, username = excluded.username,
               first_name = excluded.first_name, last_name = excluded.last_name,
               last_seen_at = excluded.last_seen_at""",
            (user.id, chat.id, user.username, user.first_name, user.last_name,
             timestamp, timestamp),
        )
        db.execute(
            "INSERT INTO interactions (user_id, kind, name, occurred_at) VALUES (?, ?, ?, ?)",
            (user.id, kind, name, timestamp),
        )


def get_task(path: Path, user_id: int, task_id: int) -> sqlite3.Row | None:
    with database(path) as db:
        return db.execute(
            "SELECT * FROM tasks WHERE task_number = ? AND user_id = ? AND status = 'active'",
            (task_id, user_id),
        ).fetchone()


def get_task_by_id(path: Path, user_id: int, internal_id: int) -> sqlite3.Row | None:
    """Кнопки используют неизменяемый внутренний ID, даже если номер переиспользован."""
    with database(path) as db:
        return db.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (internal_id, user_id)
        ).fetchone()


def add_task(
    path: Path, user_id: int, chat_id: int, title: str, priority: str,
    due_at: str | None, reminder_minutes: int,
) -> int:
    """Создаёт задачу под следующим номером открытого списка."""
    with database(path) as db:
        db.execute("BEGIN IMMEDIATE")
        renumber_active_tasks(db, user_id)
        number = db.execute(
            "SELECT COUNT(*) + 1 FROM tasks WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()[0]
        cursor = db.execute(
            "INSERT INTO tasks (user_id, task_number, chat_id, title, priority, due_at, "
            "status, created_at, reminder_minutes) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (user_id, number, chat_id, title, priority, due_at, to_db(now_utc()), reminder_minutes),
        )
        record_task_action(db, user_id, cursor.lastrowid, number, title, "created")
    return number


class WeatherError(Exception):
    """Ожидаемая ошибка получения или чтения данных о погоде."""


def get_location(path: Path, user_id: int) -> sqlite3.Row | None:
    with database(path) as db:
        return db.execute(
            "SELECT * FROM user_locations WHERE user_id = ?", (user_id,)
        ).fetchone()


def save_location(path: Path, user_id: int, location: dict) -> None:
    with database(path) as db:
        db.execute(
            """INSERT INTO user_locations
               (user_id, city_name, admin1, country, latitude, longitude)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               city_name = excluded.city_name, admin1 = excluded.admin1,
               country = excluded.country,
               latitude = excluded.latitude, longitude = excluded.longitude""",
            (user_id, location["name"], location.get("admin1") or "", location["country"],
             location["latitude"], location["longitude"]),
        )


async def fetch_json(url: str, params: dict) -> dict:
    """Загружает JSON с ограничением времени; сетевые сбои не прерывают работу задач."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherError("Сервис погоды сейчас недоступен. Попробуйте позже.") from exc
    if not isinstance(data, dict) or data.get("error"):
        raise WeatherError("Сервис погоды вернул ошибку. Попробуйте позже.")
    return data


async def search_cities(name: str) -> list[dict]:
    data = await fetch_json(
        GEOCODING_URL,
        {"name": name, "count": 10, "language": "ru", "format": "json"},
    )
    results = data.get("results", [])
    if not isinstance(results, list):
        raise WeatherError("Не удалось прочитать список городов. Попробуйте позже.")
    cities = []
    seen = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            latitude = float(item["latitude"])
            longitude = float(item["longitude"])
            city_name = str(item["name"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not city_name or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        admin1 = str(item.get("admin1") or "").strip()
        country = str(item.get("country") or "").strip()
        # API может вернуть несколько точек с одинаковой подписью: оставляем одну.
        label_key = (city_name.casefold(), admin1.casefold(), country.casefold())
        if label_key in seen:
            continue
        seen.add(label_key)
        cities.append({
            "name": city_name,
            "admin1": admin1,
            "country": country,
            "latitude": latitude,
            "longitude": longitude,
        })
    # Точное совпадение с запросом показываем первым; порядок API внутри групп сохраняем.
    cities.sort(key=lambda city: city["name"].casefold() != name.strip().casefold())
    return cities[:5]


def location_label(location: dict | sqlite3.Row) -> str:
    parts = [location["name"] if isinstance(location, dict) else location["city_name"]]
    admin1 = location.get("admin1", "") if isinstance(location, dict) else location["admin1"]
    if admin1 not in {"", parts[0]}:
        parts.append(admin1)
    if location["country"]:
        parts.append(location["country"])
    return ", ".join(parts)


async def select_city(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    name = name.strip()
    # При ошибке или отсутствии результатов можно сразу ввести другое название.
    context.user_data["flow"] = "city_name"
    if not 2 <= len(name) <= 100:
        await update.effective_message.reply_text("Введите название города от 2 до 100 символов.")
        return
    try:
        cities = await search_cities(name)
    except WeatherError as exc:
        LOGGER.warning("Ошибка поиска города: %s", exc)
        await update.effective_message.reply_text(str(exc))
        return
    if not cities:
        await update.effective_message.reply_text("Город не найден. Уточните название.")
        return
    if len(cities) == 1:
        save_location(db_path(context), update.effective_user.id, cities[0])
        context.user_data.clear()
        await update.effective_message.reply_text(
            f"✅ Город выбран: {location_label(cities[0])}.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🌤 Показать погоду", callback_data="weather")
            ]]),
        )
        return
    context.user_data["flow"] = "city_pick"
    context.user_data["city_candidates"] = cities
    # Токен не даёт старой кнопке выбрать город из нового списка поиска.
    context.user_data["city_search_token"] = secrets.token_hex(4)
    buttons = [
        [InlineKeyboardButton(
            location_label(city)[:64],
            callback_data=f"citypick:{context.user_data['city_search_token']}:{index}",
        )]
        for index, city in enumerate(cities)
    ]
    await update.effective_message.reply_text(
        "Выберите город:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def city_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_only(update):
        return
    if context.args:
        context.user_data.clear()
        await select_city(update, context, " ".join(context.args))
    else:
        await begin_city(update, context)


async def begin_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    context.user_data["flow"] = "city_name"
    await update.effective_message.reply_text("🏙 Напишите название города. Например: Москва.")


def first_daily(daily: dict, key: str) -> float | int | None:
    values = daily.get(key)
    return values[0] if isinstance(values, list) and values else None


def temp_text(value: float | int) -> str:
    return f"{value:+g} °C"


def weather_icon(code: int) -> str:
    """Подбирает значок по коду Open-Meteo, а не показывает одно солнце при любой погоде."""
    if code == 0:
        return "☀️"
    if code in {1, 2, 3}:
        return "🌤" if code != 3 else "☁️"
    if code in {45, 48}:
        return "🌫"
    if code in {71, 73, 75, 77, 85, 86}:
        return "❄️"
    if code in {95, 96, 97, 99}:
        return "⛈"
    if code in WEATHER_CODES:
        return "🌧"
    return "🌡"


async def weather_text(location: sqlite3.Row, *, brief: bool = False) -> str:
    data = await fetch_json(
        FORECAST_URL,
        {
            "latitude": location["latitude"], "longitude": location["longitude"],
            "current": "temperature_2m,precipitation,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "auto", "forecast_days": 1,
        },
    )
    try:
        current, daily = data["current"], data["daily"]
        current_temp = float(current["temperature_2m"])
        low = float(first_daily(daily, "temperature_2m_min"))
        high = float(first_daily(daily, "temperature_2m_max"))
        current_code = int(current["weather_code"])
        day_code = int(first_daily(daily, "weather_code"))
        precipitation = float(current["precipitation"])
        probability = first_daily(daily, "precipitation_probability_max")
        chance = f"{float(probability):g}%" if probability is not None else "нет данных"
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise WeatherError("Не удалось прочитать прогноз. Попробуйте позже.") from exc
    current_condition = WEATHER_CODES.get(current_code, "условия неизвестны")
    day_condition = WEATHER_CODES.get(day_code, "условия неизвестны")
    city = location["city_name"]
    if brief:
        return (
            f"{weather_icon(current_code)} {city}: сейчас {temp_text(current_temp)}, {current_condition}. "
            f"Сегодня {temp_text(low)}…{temp_text(high)}, {day_condition}; "
            f"вероятность осадков до {chance}."
        )
    return (
        f"{weather_icon(current_code)} Погода: {location_label(location)}\n"
        f"🌡 Сейчас: {temp_text(current_temp)}, {current_condition}; осадки {precipitation:g} мм.\n"
        f"📅 Сегодня: {temp_text(low)}…{temp_text(high)}, {day_condition}; "
        f"вероятность осадков до {chance}."
    )


async def send_weather(update: Update, context: ContextTypes.DEFAULT_TYPE, *, brief: bool = False) -> None:
    location = get_location(db_path(context), update.effective_user.id)
    if location is None:
        message = "Погода: выберите город через /city." if brief else "Сначала выберите город через /city."
    else:
        try:
            message = await weather_text(location, brief=brief)
        except WeatherError as exc:
            LOGGER.warning("Не удалось получить погоду для пользователя %s: %s", update.effective_user.id, exc)
            message = "Погода сейчас недоступна." if brief else str(exc)
    city_button = "🏙 Выбрать город" if location is None else "🏙 Сменить город"
    markup = None if brief else InlineKeyboardMarkup([[
        InlineKeyboardButton(city_button, callback_data="change_city")
    ]])
    await update.effective_message.reply_text(message, reply_markup=markup)


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await private_only(update):
        await send_weather(update, context)


def parse_task_id(value: str) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except ValueError:
        return None


def parse_due(value: str, tz: ZoneInfo) -> datetime | None:
    if value.strip().casefold() in {"нет", "-", "без срока"}:
        return None
    # Round-trip detects invalid local times around daylight-saving transitions.
    naive = datetime.strptime(value.strip(), DATETIME_FORMAT)
    local = naive.replace(tzinfo=tz)
    round_trip = local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if round_trip != naive:
        raise ValueError("Такого местного времени нет")
    if local.astimezone(timezone.utc) <= now_utc():
        raise ValueError("Срок должен быть в будущем")
    return local.astimezone(timezone.utc)


def priority_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{PRIORITY_ICONS[code]} {label}", callback_data=f"{prefix}:{code}")]
         for code, label in PRIORITIES.items()]
    )


def reminder_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"{prefix}:{minutes}")]
         for minutes, label in REMINDERS.items()]
    )


def due_choice_keyboard(mode: str, tz: ZoneInfo) -> InlineKeyboardMarkup:
    """Быстрый выбор дня; «сегодня» скрывается, если день почти закончился."""
    now = now_utc().astimezone(tz)
    rows = []
    if (now + timedelta(minutes=1)).date() == now.date():
        rows.append([InlineKeyboardButton("📅 Сегодня", callback_data=f"dueday:{mode}:today")])
    rows.append([InlineKeyboardButton("📆 Завтра", callback_data=f"dueday:{mode}:tomorrow")])
    rows.append([
        InlineKeyboardButton("Без срока", callback_data=f"dueday:{mode}:none"),
        InlineKeyboardButton("✍️ Ввести дату", callback_data=f"dueday:{mode}:manual"),
    ])
    return InlineKeyboardMarkup(rows)


def due_time_keyboard(mode: str, day: date, tz: ZoneInfo) -> InlineKeyboardMarkup:
    """Предлагаем только те стандартные часы, которые ещё не прошли."""
    rows = []
    for clock in ("09:00", "12:00", "18:00"):
        try:
            parse_due(f"{day:%d.%m.%Y} {clock}", tz)
        except ValueError:
            continue
        rows.append([InlineKeyboardButton(
            f"🕒 {clock}", callback_data=f"duetime:{mode}:{clock.replace(':', '')}",
        )])
    rows.append([InlineKeyboardButton("✍️ Другое время", callback_data=f"duetime:{mode}:manual")])
    return InlineKeyboardMarkup(rows)


def task_text(task: sqlite3.Row, tz: ZoneInfo) -> str:
    mark = "✅" if task["status"] == "done" else "▫️"
    return (
        f"{mark} #{task['task_number']} {task['title']}\n"
        f"{PRIORITY_ICONS[task['priority']]} Приоритет: {PRIORITIES[task['priority']]} "
        f"· 📅 Срок: {format_due(task['due_at'], tz)}\n"
        f"🔔 Напоминание: {REMINDERS.get(task['reminder_minutes'], 'выключено')}"
    )


def task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[  # ID проверяется вместе с владельцем при каждом нажатии.
        InlineKeyboardButton("✅ Готово", callback_data=f"done:{task_id}"),
        InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{task_id}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"delete:{task_id}"),
    ]])


async def private_only(update: Update) -> bool:
    chat = update.effective_chat
    if chat and chat.type == ChatType.PRIVATE:
        return True
    if update.callback_query:
        await update.callback_query.answer("Напишите боту в личные сообщения.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("Напишите боту в личные сообщения.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_only(update):
        return
    # Настройка конкретного чата перекрывает старые персональные настройки меню.
    await context.bot.set_my_commands(
        MENU_COMMANDS, scope=BotCommandScopeChat(update.effective_chat.id)
    )
    await context.bot.set_chat_menu_button(
        chat_id=update.effective_chat.id, menu_button=MenuButtonCommands()
    )
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Привет! Я помогу вести задачи. Все команды есть в «Меню» слева от строки ввода.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить", callback_data="new")],
            [InlineKeyboardButton("📋 Задачи", callback_data="list"),
             InlineKeyboardButton("📅 Сегодня", callback_data="today")],
            [InlineKeyboardButton("✅ Выполнить", callback_data="done_picker"),
             InlineKeyboardButton("🌤 Погода", callback_data="weather")],
        ]),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_only(update):
        return
    await update.effective_message.reply_text(
        "/start — главное меню\n/help — помощь\n/add — новая задача\n"
        "/tasks — открытые задачи\n/today — на сегодня, включая новые без срока\n"
        "/week — созданные за 7 дней\n/search — поиск задач\n/history — история действий\n"
        "/city — выбрать город\n/weather — погода и прогноз\n"
        "/done — выбрать задачу кнопкой; /done ID — выполнить по номеру\n"
        "/edit ID — изменить\n/delete ID — удалить\n"
        "/stats — статистика\n/cancel — отменить ввод\n\n"
        "Срок можно выбрать кнопками или ввести как ДД.ММ.ГГГГ ЧЧ:ММ. "
        "Напоминание придёт в личный чат."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_only(update):
        return
    context.user_data.clear()
    await update.effective_message.reply_text("Действие отменено.")


async def begin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_only(update):
        return
    context.user_data.clear()
    context.user_data["flow"] = "add_title"
    await update.effective_message.reply_text("📝 Название задачи? (до 200 символов)")


async def ask_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["flow"] = "add_due_choice"
    await update.effective_message.reply_text(
        f"📅 Когда выполнить? Часовой пояс: {local_tz(context).key}. "
        "Выберите день или введите ДД.ММ.ГГГГ ЧЧ:ММ.",
        reply_markup=due_choice_keyboard("add", local_tz(context)),
    )


async def apply_due(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    mode: str, due: datetime | None) -> None:
    """Завершает выбор срока одинаково для новой и существующей задачи."""
    context.user_data.pop("due_day", None)
    if mode == "add":
        context.user_data["due_at"] = to_db(due)
        await ask_reminder(update, context)
    else:
        await update_field(update, context, "due_at", to_db(due))


async def accept_due_time(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          mode: str, raw_time: str) -> None:
    """Соединяет выбранный день со временем и повторно проверяет будущий срок."""
    try:
        day = date.fromisoformat(context.user_data["due_day"])
        clock = datetime.strptime(raw_time, "%H:%M").strftime("%H:%M")
        due = parse_due(f"{day:%d.%m.%Y} {clock}", local_tz(context))
    except (KeyError, ValueError):
        await update.effective_message.reply_text(
            "Время неверное или уже прошло. Введите будущее время как ЧЧ:ММ "
            "или начните выбор срока заново."
        )
        return
    await apply_due(update, context, mode, due)


async def ask_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["flow"] = "add_reminder"
    if context.user_data.get("due_at") is None:
        await update.effective_message.reply_text(
            "🔔 Без срока напоминание недоступно.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Сохранить задачу", callback_data="reminder:0")
            ]]),
        )
        return
    await update.effective_message.reply_text(
        "🔔 Когда напомнить?", reply_markup=reminder_keyboard("reminder")
    )


async def show_tasks(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, today: bool = False, offset: int = 0
) -> None:
    if not await private_only(update):
        return
    user_id = update.effective_user.id
    with database(db_path(context)) as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND status = 'active' "
            "ORDER BY task_number ASC",
            (user_id,),
        ).fetchall()
    if today:
        # Просроченные задачи и задачи без срока, созданные сегодня, видны здесь.
        tz = local_tz(context)
        current_day = now_utc().astimezone(tz).date()
        end = datetime.combine(
            current_day + timedelta(days=1), datetime.min.time(), tz
        ).astimezone(timezone.utc)
        rows = [
            row for row in rows
            if (row["due_at"] and from_db(row["due_at"]) < end)
            or (
                not row["due_at"]
                and from_db(row["created_at"]).astimezone(tz).date() == current_day
            )
        ]
        # Погода показывается и при пустом списке задач; на следующих страницах не повторяется.
        if offset <= 0:
            await send_weather(update, context, brief=True)
    if not rows:
        await update.effective_message.reply_text("На сегодня задач нет." if today else "Открытых задач нет.")
        return
    offset = min(max(offset, 0), ((len(rows) - 1) // 10) * 10)
    page = rows[offset:offset + 10]
    await update.effective_message.reply_text(
        ("📅 Сегодня и просроченные" if today else "📋 Открытые задачи")
        + f" ({offset + 1}–{offset + len(page)} из {len(rows)}):"
    )
    # По одной задаче на сообщение: кнопки всегда связаны с нужным ID.
    for row in page:
        await update.effective_message.reply_text(
            task_text(row, local_tz(context)), reply_markup=task_keyboard(row["id"])
        )
    navigation = []
    scope = "today" if today else "all"
    if offset > 0:
        navigation.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{scope}:{max(0, offset - 10)}"))
    if offset + 10 < len(rows):
        navigation.append(InlineKeyboardButton("Дальше ➡️", callback_data=f"page:{scope}:{offset + 10}"))
    if navigation:
        await update.effective_message.reply_text("Страницы:", reply_markup=InlineKeyboardMarkup([navigation]))


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_tasks(update, context)


async def show_done_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, offset: int = 0
) -> None:
    """Даёт выбрать задачу по названию; кнопка несёт постоянный внутренний ID."""
    if not await private_only(update):
        return
    with database(db_path(context)) as db:
        rows = db.execute(
            "SELECT id, task_number, title FROM tasks "
            "WHERE user_id = ? AND status = 'active' ORDER BY task_number",
            (update.effective_user.id,),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("Открытых задач нет.")
        return
    offset = min(max(offset, 0), ((len(rows) - 1) // PAGE_SIZE) * PAGE_SIZE)
    page = rows[offset:offset + PAGE_SIZE]
    buttons = [[InlineKeyboardButton(
        f"✅ №{row['task_number']} · {row['title'][:43]}",
        callback_data=f"done:{row['id']}",
    )] for row in page]
    navigation = []
    if offset > 0:
        navigation.append(InlineKeyboardButton(
            "⬅️ Назад", callback_data=f"donepage:{offset - PAGE_SIZE}",
        ))
    if offset + PAGE_SIZE < len(rows):
        navigation.append(InlineKeyboardButton(
            "Дальше ➡️", callback_data=f"donepage:{offset + PAGE_SIZE}",
        ))
    if navigation:
        buttons.append(navigation)
    await update.effective_message.reply_text(
        f"✅ Какую задачу выполнить? ({offset + 1}–{offset + len(page)} из {len(rows)})",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_tasks(update, context, today=True)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_only(update):
        return
    with database(db_path(context)) as db:
        counts = db.execute(
            "SELECT status, COUNT(*) AS total FROM tasks WHERE user_id = ? GROUP BY status",
            (update.effective_user.id,),
        ).fetchall()
    values = {row["status"]: row["total"] for row in counts}
    await update.effective_message.reply_text(
        f"Открыто: {values.get('active', 0)} · Выполнено: {values.get('done', 0)}"
    )


def parse_search(raw: str) -> dict:
    """Пример: молоко статус=открыта дата=12.10.2026."""
    status_names = {
        "открыта": "active", "открытые": "active", "active": "active",
        "выполнена": "done", "выполненные": "done", "done": "done",
        "все": None, "all": None,
    }
    result = {"title": "", "status": None, "date": None}
    title_parts = []
    for word in raw.split():
        key, separator, value = word.partition("=")
        key = key.casefold()
        if separator and key in {"статус", "status"}:
            if value.casefold() not in status_names:
                raise ValueError("Статус: открыта, выполнена или все.")
            result["status"] = status_names[value.casefold()]
        elif separator and key in {"дата", "date"}:
            try:
                day = datetime.strptime(value, "%d.%m.%Y").date()
                if day.strftime("%d.%m.%Y") != value:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("Дата: ДД.ММ.ГГГГ.") from exc
            result["date"] = day
        elif separator:
            raise ValueError(f"Неизвестный фильтр: {key}.")
        else:
            title_parts.append(word)
    result["title"] = " ".join(title_parts)
    if len(result["title"]) > 200:
        raise ValueError("Название для поиска слишком длинное (до 200 символов).")
    return result


def task_matches_date(task: sqlite3.Row, day: date, tz: ZoneInfo) -> bool:
    # Дата относится к созданию, сроку или выполнению задачи.
    return any(
        from_db(task[field]).astimezone(tz).date() == day
        for field in ("created_at", "due_at", "completed_at") if task[field]
    )


def find_tasks(path: Path, user_id: int, criteria: dict, tz: ZoneInfo) -> list[sqlite3.Row]:
    with database(path) as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at ASC, id ASC",
            (user_id,),
        ).fetchall()
    return [
        row for row in rows
        if (criteria["status"] is None or row["status"] == criteria["status"])
        and criteria["title"].casefold() in row["title"].casefold()
        and (criteria["date"] is None or task_matches_date(row, criteria["date"], tz))
    ]


def recent_tasks(path: Path, user_id: int, tz: ZoneInfo) -> list[sqlite3.Row]:
    """Задачи, созданные сегодня и за шесть предыдущих календарных дней."""
    today = now_utc().astimezone(tz).date()
    start = datetime.combine(today - timedelta(days=6), datetime.min.time(), tz)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tz)
    with database(path) as db:
        return db.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at ASC, id ASC",
            (user_id, to_db(start), to_db(end)),
        ).fetchall()


def get_history(path: Path, user_id: int) -> list[sqlite3.Row]:
    with database(path) as db:
        return db.execute(
            "SELECT * FROM task_history WHERE user_id = ? "
            "ORDER BY occurred_at DESC, id DESC",
            (user_id,),
        ).fetchall()


def short(value: str | None, length: int = 55) -> str:
    value = value or "нет"
    return value if len(value) <= length else value[:length - 1] + "…"


def history_value(field: str | None, value: str | None, tz: ZoneInfo) -> str:
    if field == "due_at":
        return format_due(value, tz)
    if field == "priority":
        return PRIORITIES.get(value, value or "нет")
    if field == "reminder_minutes":
        return REMINDERS.get(int(value), value) if value is not None else "нет"
    return short(value, 40)


def history_text(row: sqlite3.Row, tz: ZoneInfo) -> str:
    when = from_db(row["occurred_at"]).astimezone(tz).strftime("%d.%m.%Y %H:%M")
    number = f" №{row['task_number']}" if row["task_number"] is not None else ""
    title = f"«{short(row['task_title'])}»"
    actions = {"created": "➕ Создана", "edited": "✏️ Изменена",
               "completed": "✅ Выполнена", "deleted": "🗑 Удалена"}
    line = f"{when} · {actions[row['action']]}{number} {title}"
    if row["action"] == "edited":
        labels = {"title": "название", "priority": "приоритет",
                  "due_at": "срок", "reminder_minutes": "напоминание"}
        field = row["field_name"]
        before = history_value(field, row["old_value"], tz)
        after = history_value(field, row["new_value"], tz)
        line += f"\n{labels.get(field, field)}: {before} → {after}"
    return line


def archive_task_text(task: sqlite3.Row, tz: ZoneInfo) -> str:
    status = f"▫️ Открыта №{task['task_number']}" if task["status"] == "active" else "✅ Выполнена"
    created = from_db(task["created_at"]).astimezone(tz).strftime(DATETIME_FORMAT)
    text = f"{status}: {task['title']}\nСоздана: {created} · Срок: {format_due(task['due_at'], tz)}"
    if task["completed_at"]:
        completed = from_db(task["completed_at"]).astimezone(tz).strftime(DATETIME_FORMAT)
        text += f"\nВыполнена: {completed}"
    return text


async def show_collection(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, *, offset: int = 0,
) -> None:
    if not await private_only(update):
        return
    user_id, tz = update.effective_user.id, local_tz(context)
    if kind == "history":
        rows, heading, empty = get_history(db_path(context), user_id), "История действий", "История пока пуста."
    elif kind == "week":
        rows = recent_tasks(db_path(context), user_id, tz)
        heading, empty = "Задачи за последние 7 дней", "За последние 7 дней задач нет."
    elif kind == "search":
        criteria = context.user_data.get("search_criteria")
        if criteria is None:
            await update.effective_message.reply_text("Поиск устарел. Повторите /search.")
            return
        rows = find_tasks(db_path(context), user_id, criteria, tz)
        heading, empty = "Результаты поиска", "Задачи не найдены."
    else:
        return
    if not rows:
        await update.effective_message.reply_text(empty)
        return
    offset = min(max(offset, 0), ((len(rows) - 1) // PAGE_SIZE) * PAGE_SIZE)
    page = rows[offset:offset + PAGE_SIZE]
    await update.effective_message.reply_text(
        f"{heading} ({offset + 1}–{offset + len(page)} из {len(rows)}):"
    )
    for row in page:
        if kind == "history":
            await update.effective_message.reply_text(history_text(row, tz))
        else:
            markup = task_keyboard(row["id"]) if row["status"] == "active" else None
            await update.effective_message.reply_text(archive_task_text(row, tz), reply_markup=markup)
    buttons = []
    token = context.user_data.get("search_token") if kind == "search" else None
    suffix = f":{token}" if token else ""
    if offset > 0:
        buttons.append(InlineKeyboardButton(
            "⬅️ Назад", callback_data=f"browse:{kind}:{offset - PAGE_SIZE}{suffix}",
        ))
    if offset + PAGE_SIZE < len(rows):
        buttons.append(InlineKeyboardButton(
            "Дальше ➡️", callback_data=f"browse:{kind}:{offset + PAGE_SIZE}{suffix}",
        ))
    if buttons:
        await update.effective_message.reply_text("Страницы:", reply_markup=InlineKeyboardMarkup([buttons]))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_collection(update, context, "history")


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_collection(update, context, "week")


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    if not raw.strip():
        await update.effective_message.reply_text("Введите название или фильтр: статус=открыта, дата=ДД.ММ.ГГГГ.")
        return
    try:
        criteria = parse_search(raw)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    context.user_data["search_criteria"] = criteria
    context.user_data["search_token"] = secrets.token_hex(4)
    context.user_data.pop("flow", None)
    await show_collection(update, context, "search")


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_only(update):
        return
    context.user_data.clear()
    context.user_data["flow"] = "search_query"
    if context.args:
        await run_search(update, context, " ".join(context.args))
    else:
        await update.effective_message.reply_text(
            "Что найти? Напишите название и/или фильтры.\n"
            "Например: молоко статус=открыта дата=12.10.2026\n"
            "Статус: открыта, выполнена, все. Дата: ДД.ММ.ГГГГ."
        )


async def command_with_id(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    if not await private_only(update):
        return
    if not context.args:
        context.user_data.clear()
        context.user_data["flow"] = f"{action}_id"
        await update.effective_message.reply_text(f"Укажите ID задачи. Например: /{action} 12")
        return
    task_id = parse_task_id(context.args[0])
    if task_id is None:
        await update.effective_message.reply_text("Нужен положительный числовой ID задачи.")
        return
    await dispatch_action(update, context, action, task_id)


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        context.user_data.clear()
        await show_done_picker(update, context)
    else:
        await command_with_id(update, context, "done")


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await command_with_id(update, context, "edit")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await command_with_id(update, context, "delete")


async def dispatch_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, task_id: int,
    *, internal: bool = False,
) -> None:
    lookup = get_task_by_id if internal else get_task
    task = lookup(db_path(context), update.effective_user.id, task_id)
    if not task:
        await update.effective_message.reply_text("Задача не найдена.")
        return
    number = task["task_number"]
    internal_id = task["id"]
    if action == "done":
        if task["status"] == "done":
            await update.effective_message.reply_text("Задача уже выполнена.")
            return
        with database(db_path(context)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE tasks SET status = 'done', task_number = NULL, completed_at = ? "
                "WHERE id = ? AND user_id = ?",
                (to_db(now_utc()), internal_id, update.effective_user.id),
            )
            record_task_action(
                db, update.effective_user.id, internal_id, number,
                task["title"], "completed",
            )
            renumber_active_tasks(db, update.effective_user.id)
        await update.effective_message.reply_text(
            f"✅ Задача #{number} «{task['title']}» выполнена."
        )
    elif action == "edit":
        if task["status"] == "done":
            await update.effective_message.reply_text("Выполненную задачу нельзя изменить.")
            return
        context.user_data.clear()
        context.user_data["edit_id"] = internal_id
        await update.effective_message.reply_text(
            f"Что изменить в задаче #{number}?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Название", callback_data=f"field:title:{internal_id}"),
                 InlineKeyboardButton("Срок", callback_data=f"field:due:{internal_id}")],
                [InlineKeyboardButton("Приоритет", callback_data=f"field:priority:{internal_id}"),
                 InlineKeyboardButton("Напоминание", callback_data=f"field:reminder:{internal_id}")],
            ]),
        )
    elif action == "delete":
        task_label = f"задачу #{number}" if number is not None else "выполненную задачу"
        await update.effective_message.reply_text(
            f"Удалить {task_label} «{task['title']}»?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Да, удалить", callback_data=f"confirm:{internal_id}"),
                InlineKeyboardButton("Отмена", callback_data="cancel_delete"),
            ]]),
        )


async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_only(update):
        return
    flow = context.user_data.get("flow")
    value = update.effective_message.text.strip()
    if not flow:
        await update.effective_message.reply_text("Выберите команду в «Меню» или отправьте /help.")
        return
    if flow == "city_name":
        await select_city(update, context, value)
        return
    if flow == "city_pick":
        await update.effective_message.reply_text("Выберите город кнопкой выше или начните поиск через /city.")
        return
    if flow == "search_query":
        await run_search(update, context, value)
        return
    if flow.endswith("_id"):
        task_id = parse_task_id(value)
        if task_id is None:
            await update.effective_message.reply_text("Нужен положительный числовой ID.")
            return
        context.user_data.clear()
        await dispatch_action(update, context, flow.removesuffix("_id"), task_id)
        return
    if flow in {"add_title", "edit_title"}:
        if not value or len(value) > 200:
            await update.effective_message.reply_text("Название должно содержать от 1 до 200 символов.")
            return
        if flow == "add_title":
            context.user_data["title"] = value
            context.user_data["flow"] = "add_priority"
            await update.effective_message.reply_text(
                "🎯 Приоритет задачи?", reply_markup=priority_keyboard("priority")
            )
        else:
            await update_field(update, context, "title", value)
        return
    if flow in {"add_due_time", "edit_due_time"}:
        await accept_due_time(update, context, flow.split("_", 1)[0], value)
        return
    if flow in {"add_due_choice", "add_due_manual", "edit_due_choice", "edit_due_manual",
                "add_due", "edit_due"}:
        try:
            due = parse_due(value, local_tz(context))
        except ValueError:
            await update.effective_message.reply_text("Неверный срок. Введите будущую дату как ДД.ММ.ГГГГ ЧЧ:ММ или «нет».")
            return
        await apply_due(update, context, flow.split("_", 1)[0], due)
        return


async def update_field(
    update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, value: str | int | None
) -> None:
    task_id = context.user_data.get("edit_id")
    task = get_task_by_id(db_path(context), update.effective_user.id, task_id) if task_id else None
    if not task or task["status"] != "active":
        context.user_data.clear()
        await update.effective_message.reply_text("Задача не найдена или уже выполнена.")
        return
    # Поле выбирается только из фиксированного списка, значения передаются параметрами.
    if field not in {"title", "priority", "due_at", "reminder_minutes"}:
        raise ValueError("Недопустимое поле")
    if task[field] == value:
        context.user_data.clear()
        await update.effective_message.reply_text("Изменений нет.")
        return
    if field == "due_at" and value is None:
        reminder = 0
    elif field == "reminder_minutes" and task["due_at"] is None:
        reminder = 0
        value = 0
    else:
        reminder = None
    with database(db_path(context)) as db:
        if field in {"title", "priority"}:
            db.execute(
                f"UPDATE tasks SET {field} = ? WHERE id = ? AND user_id = ?",
                (value, task_id, update.effective_user.id),
            )
        elif reminder is None:
            db.execute(
                f"UPDATE tasks SET {field} = ?, reminder_sent_at = NULL WHERE id = ? AND user_id = ?",
                (value, task_id, update.effective_user.id),
            )
        else:
            db.execute(
                f"UPDATE tasks SET {field} = ?, reminder_minutes = ?, reminder_sent_at = NULL "
                "WHERE id = ? AND user_id = ?",
                (value, reminder, task_id, update.effective_user.id),
            )
        record_task_action(
            db, update.effective_user.id, task_id, task["task_number"],
            str(value) if field == "title" else task["title"], "edited",
            field_name=field,
            old_value=str(task[field]) if task[field] is not None else None,
            new_value=str(value) if value is not None else None,
        )
    context.user_data.clear()
    await update.effective_message.reply_text(f"✅ Задача #{task['task_number']} обновлена.")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await private_only(update):
        return
    await query.answer()
    data = query.data or ""
    if data == "new":
        await begin_add(update, context)
        return
    if data == "help":
        await help_command(update, context)
        return
    if data in {"list", "today"}:
        await show_tasks(update, context, today=data == "today")
        return
    if data == "done_picker":
        context.user_data.clear()
        await show_done_picker(update, context)
        return
    if data.startswith("donepage:"):
        try:
            offset = int(data.split(":", 1)[1])
        except ValueError:
            return
        await show_done_picker(update, context, offset=offset)
        return
    if data == "weather":
        await send_weather(update, context)
        return
    if data == "change_city":
        await begin_city(update, context)
        return
    if data.startswith("citypick:"):
        candidates = context.user_data.get("city_candidates")
        token = None
        try:
            _, token, raw_index = data.split(":", 2)
            index = int(raw_index)
            city = candidates[index] if candidates is not None and index >= 0 else None
        except (TypeError, ValueError, IndexError):
            city = None
        if (context.user_data.get("flow") != "city_pick"
                or token != context.user_data.get("city_search_token") or city is None):
            await query.message.reply_text("Выбор города устарел. Повторите /city.")
            return
        save_location(db_path(context), update.effective_user.id, city)
        context.user_data.clear()
        await query.message.reply_text(
            f"✅ Город выбран: {location_label(city)}.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🌤 Показать погоду", callback_data="weather")
            ]]),
        )
        return
    if data.startswith("page:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[1] in {"all", "today"}:
            try:
                offset = int(parts[2])
            except ValueError:
                return
            await show_tasks(update, context, today=parts[1] == "today", offset=offset)
        return
    if data.startswith("browse:"):
        parts = data.split(":")
        if len(parts) not in {3, 4} or parts[1] not in {"history", "week", "search"}:
            await query.message.reply_text("Страница устарела. Повторите команду.")
            return
        try:
            offset = int(parts[2])
        except ValueError:
            await query.message.reply_text("Страница устарела. Повторите команду.")
            return
        if parts[1] == "search" and (
            len(parts) != 4 or parts[3] != context.user_data.get("search_token")
        ):
            await query.message.reply_text("Поиск устарел. Повторите /search.")
            return
        await show_collection(update, context, parts[1], offset=offset)
        return
    if data == "cancel_delete":
        await query.message.reply_text("Удаление отменено.")
        return
    if data.startswith(("done:", "edit:", "delete:")):
        action, raw_id = data.split(":", 1)
        task_id = parse_task_id(raw_id)
        if task_id:
            await dispatch_action(update, context, action, task_id, internal=True)
        return
    if data.startswith("confirm:"):
        task_id = parse_task_id(data.split(":", 1)[1])
        if not task_id:
            return
        task = get_task_by_id(db_path(context), update.effective_user.id, task_id)
        if not task:
            await query.message.reply_text("Задача не найдена.")
            return
        with database(db_path(context)) as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, update.effective_user.id)
            )
            if cursor.rowcount:
                record_task_action(
                    db, update.effective_user.id, task_id, task["task_number"],
                    task["title"], "deleted", old_value=task["status"],
                )
                renumber_active_tasks(db, update.effective_user.id)
        deleted_label = (
            f"Задача #{task['task_number']}" if task["task_number"] is not None
            else "Выполненная задача"
        )
        await query.message.reply_text(
            f"🗑 {deleted_label} удалена." if cursor.rowcount else "Задача не найдена."
        )
        return
    if data.startswith("priority:") and context.user_data.get("flow") == "add_priority":
        priority = data.split(":", 1)[1]
        if priority in PRIORITIES:
            context.user_data["priority"] = priority
            await ask_due(update, context)
        return
    if data.startswith("dueday:"):
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"add", "edit"}:
            return
        _, mode, choice = parts
        if context.user_data.get("flow") != f"{mode}_due_choice":
            await query.message.reply_text("Выбор срока устарел. Начните его заново.")
            return
        if choice == "none":
            await apply_due(update, context, mode, None)
        elif choice == "manual":
            context.user_data["flow"] = f"{mode}_due_manual"
            await query.message.reply_text(
                f"✍️ Введите срок ({local_tz(context).key}): ДД.ММ.ГГГГ ЧЧ:ММ."
            )
        elif choice in {"today", "tomorrow"}:
            day = now_utc().astimezone(local_tz(context)).date()
            if choice == "tomorrow":
                day += timedelta(days=1)
            context.user_data["due_day"] = day.isoformat()
            context.user_data["flow"] = f"{mode}_due_time"
            await query.message.reply_text(
                f"🕒 Выберите время на {day:%d.%m.%Y} "
                "или напишите его как ЧЧ:ММ.",
                reply_markup=due_time_keyboard(mode, day, local_tz(context)),
            )
        return
    if data.startswith("duetime:"):
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"add", "edit"}:
            return
        _, mode, choice = parts
        if context.user_data.get("flow") != f"{mode}_due_time":
            await query.message.reply_text("Выбор времени устарел. Начните его заново.")
            return
        if choice == "manual":
            await query.message.reply_text("✍️ Напишите время как ЧЧ:ММ.")
        elif len(choice) == 4 and choice.isdigit():
            await accept_due_time(update, context, mode, f"{choice[:2]}:{choice[2:]}")
        return
    if data == "due:none" and context.user_data.get("flow") in {"add_due", "add_due_choice"}:
        await apply_due(update, context, "add", None)
        return
    if data.startswith("reminder:") and context.user_data.get("flow") == "add_reminder":
        try:
            minutes = int(data.split(":", 1)[1])
        except ValueError:
            return
        if minutes not in REMINDERS:
            return
        if context.user_data["due_at"] is None:
            minutes = 0
        title = context.user_data["title"]
        task_number = add_task(
            db_path(context), update.effective_user.id, update.effective_chat.id,
            title, context.user_data["priority"],
            context.user_data["due_at"], minutes,
        )
        context.user_data.clear()
        await query.message.reply_text(f"✅ Задача #{task_number} «{title}» добавлена.")
        return
    if data.startswith("field:"):
        parts = data.split(":")
        if len(parts) != 3:
            return
        _, field, raw_id = parts
        task_id = parse_task_id(raw_id)
        task = get_task_by_id(db_path(context), update.effective_user.id, task_id) if task_id else None
        if not task or task["status"] != "active":
            await query.message.reply_text("Задача не найдена или уже выполнена.")
            return
        context.user_data.clear()
        context.user_data["edit_id"] = task_id
        if field == "title":
            context.user_data["flow"] = "edit_title"
            await query.message.reply_text("Новое название?")
        elif field == "due":
            context.user_data["flow"] = "edit_due_choice"
            await query.message.reply_text(
                f"📅 Новый срок ({local_tz(context).key}): выберите день "
                "или введите ДД.ММ.ГГГГ ЧЧ:ММ.",
                reply_markup=due_choice_keyboard("edit", local_tz(context)),
            )
        elif field == "priority":
            context.user_data["flow"] = "edit_priority"
            await query.message.reply_text("Новый приоритет?", reply_markup=priority_keyboard("setpriority"))
        elif field == "reminder":
            context.user_data["flow"] = "edit_reminder"
            if task["due_at"] is None:
                await query.message.reply_text("Сначала установите срок задачи.")
                context.user_data.clear()
            else:
                await query.message.reply_text("Новое напоминание?", reply_markup=reminder_keyboard("setreminder"))
        return
    if data.startswith("setpriority:") and context.user_data.get("flow") == "edit_priority":
        priority = data.split(":", 1)[1]
        if priority in PRIORITIES:
            await update_field(update, context, "priority", priority)
        return
    if data.startswith("setreminder:") and context.user_data.get("flow") == "edit_reminder":
        raw = data.split(":", 1)[1]
        try:
            minutes = int(raw)
        except ValueError:
            return
        if minutes in REMINDERS:
            await update_field(update, context, "reminder_minutes", minutes)
        return
    await query.message.reply_text("Кнопка устарела. Начните действие заново.")


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в минуту ищет несработавшие напоминания, включая пропущенные при рестарте."""
    current = now_utc()
    with database(db_path(context)) as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE status = 'active' AND due_at IS NOT NULL "
            "AND reminder_minutes > 0 AND reminder_sent_at IS NULL"
        ).fetchall()
    for row in rows:
        if from_db(row["due_at"]) - timedelta(minutes=row["reminder_minutes"]) > current:
            continue
        try:
            await context.bot.send_message(
                chat_id=row["chat_id"],
                text=f"⏰ Напоминание: #{row['task_number']} {row['title']}\nСрок: {format_due(row['due_at'], local_tz(context))}",
            )
        except Exception:
            LOGGER.exception("Не удалось отправить напоминание для задачи %s", row["id"])
            continue
        with database(db_path(context)) as db:
            db.execute(
                "UPDATE tasks SET reminder_sent_at = ? WHERE id = ? AND status = 'active' "
                "AND reminder_sent_at IS NULL",
                (to_db(now_utc()), row["id"]),
            )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Ошибка при обработке обновления", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Произошла ошибка. Попробуйте ещё раз.")
        except Exception:
            LOGGER.exception("Не удалось сообщить пользователю об ошибке")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await private_only(update):
        await update.effective_message.reply_text("Неизвестная команда. Подсказка: /help")


async def configure_menu(app: Application) -> None:
    """Регистрирует команды в постоянном меню Telegram слева от поля ввода."""
    # Публикуем общий и приватный наборы: Telegram-клиенты выбирают команды
    # по области видимости, а персональные настройки задаются при /start.
    await app.bot.set_my_commands(MENU_COMMANDS)
    await app.bot.set_my_commands(MENU_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    tz_name = os.getenv("TIMEZONE", "Europe/Moscow")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Неизвестный часовой пояс: {tz_name}") from exc
    path = Path(os.getenv("DB_PATH", "tasks.db"))
    if not path.is_absolute():
        path = BASE_DIR / path
    init_database(path)

    app: Application = ApplicationBuilder().token(token).post_init(configure_menu).build()
    app.bot_data.update({"timezone": tz, "db_path": path})
    app.add_handler(TypeHandler(Update, track_interaction), group=-1)
    for command, handler in (
        ("start", start), ("help", help_command), ("add", begin_add),
        ("tasks", tasks_command), ("today", today_command), ("done", done_command),
        ("week", week_command), ("search", search_command), ("history", history_command),
        ("city", city_command), ("weather", weather_command),
        ("edit", edit_command), ("delete", delete_command), ("stats", stats),
        ("cancel", cancel),
    ):
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_error_handler(on_error)
    if app.job_queue is None:
        raise SystemExit("Установите пакет с поддержкой job-queue из requirements.txt")
    app.job_queue.run_repeating(check_reminders, interval=60, first=5)
    LOGGER.info("Бот запущен. База: %s; часовой пояс: %s", path, tz_name)
    app.run_polling()


if __name__ == "__main__":
    main()
