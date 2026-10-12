"""Проверки сценариев, изменённых по отзывам тестировщиков."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram.constants import ChatType

import bot


FIXED_NOW = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)  # 13:00 в Москве


class FeedbackUxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "tasks.db"
        bot.init_database(self.path)
        self.message = SimpleNamespace(reply_text=AsyncMock(), text="")
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=11, type=ChatType.PRIVATE),
            effective_user=SimpleNamespace(id=11),
            effective_message=self.message,
            callback_query=None,
        )
        self.context = SimpleNamespace(
            args=[], user_data={},
            application=SimpleNamespace(bot_data={
                "db_path": self.path, "timezone": ZoneInfo("Europe/Moscow"),
            }),
        )

    async def press(self, data):
        self.update.callback_query = SimpleNamespace(
            data=data, message=self.message, answer=AsyncMock(),
        )
        await bot.callbacks(self.update, self.context)

    async def test_today_shortcut_offers_only_future_times_and_saves_task(self):
        self.context.user_data.update({
            "flow": "add_due_choice", "title": "Позвонить", "priority": "normal",
        })
        with patch.object(bot, "now_utc", return_value=FIXED_NOW):
            await self.press("dueday:add:today")
            keyboard = self.message.reply_text.await_args.kwargs["reply_markup"]
            choices = [button.text for row in keyboard.inline_keyboard for button in row]
            self.assertIn("🕒 18:00", choices)
            self.assertNotIn("🕒 09:00", choices)
            self.assertNotIn("🕒 12:00", choices)
            await self.press("duetime:add:1800")
            self.assertEqual(self.context.user_data["flow"], "add_reminder")
            self.assertEqual(bot.from_db(self.context.user_data["due_at"]),
                             datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc))
            await self.press("reminder:60")
        self.assertEqual(bot.get_task(self.path, 11, 1)["title"], "Позвонить")
        self.assertIn("«Позвонить» добавлена", self.message.reply_text.await_args.args[0])

    async def test_past_time_is_rejected_and_user_can_enter_another(self):
        self.context.user_data.update({
            "flow": "add_due_time", "due_day": "2026-10-12",
            "title": "Позвонить", "priority": "normal",
        })
        with patch.object(bot, "now_utc", return_value=FIXED_NOW):
            self.message.text = "09:00"
            await bot.text_input(self.update, self.context)
            self.assertEqual(self.context.user_data["flow"], "add_due_time")
            self.assertIn("уже прошло", self.message.reply_text.await_args.args[0])
            self.message.text = "17:30"
            await bot.text_input(self.update, self.context)
            self.assertEqual(self.context.user_data["flow"], "add_reminder")

    async def test_done_picker_uses_title_and_stable_internal_id(self):
        first = bot.add_task(self.path, 11, 11, "Купить молоко", "normal", None, 0)
        second = bot.add_task(self.path, 11, 11, "Позвонить", "normal", None, 0)
        self.assertEqual((first, second), (1, 2))
        await bot.done_command(self.update, self.context)
        keyboard = self.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertIn("№2 · Позвонить", keyboard.inline_keyboard[1][0].text)
        await self.press(keyboard.inline_keyboard[1][0].callback_data)
        self.assertIn("«Позвонить» выполнена", self.message.reply_text.await_args.args[0])
        self.assertEqual(bot.get_task(self.path, 11, 1)["title"], "Купить молоко")
        self.assertIsNone(bot.get_task(self.path, 11, 2))

    async def test_edit_due_shortcut_can_remove_due_and_reminder(self):
        due = bot.to_db(datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc))
        bot.add_task(self.path, 11, 11, "Встреча", "normal", due, 60)
        internal_id = bot.get_task(self.path, 11, 1)["id"]
        await self.press(f"field:due:{internal_id}")
        self.assertEqual(self.context.user_data["flow"], "edit_due_choice")
        await self.press("dueday:edit:none")
        task = bot.get_task(self.path, 11, 1)
        self.assertIsNone(task["due_at"])
        self.assertEqual(task["reminder_minutes"], 0)

    async def test_existing_city_survives_region_migration(self):
        old_path = Path(self.temp_dir.name) / "old.db"
        with closing(sqlite3.connect(old_path)) as db:
            db.execute("""CREATE TABLE user_locations (
                user_id INTEGER PRIMARY KEY, city_name TEXT NOT NULL, country TEXT,
                latitude REAL NOT NULL, longitude REAL NOT NULL)""")
            db.execute("INSERT INTO user_locations VALUES (11, 'Казань', 'Россия', 55.8, 49.1)")
            db.commit()
        bot.init_database(old_path)
        location = bot.get_location(old_path, 11)
        self.assertEqual((location["city_name"], location["admin1"], location["longitude"]),
                         ("Казань", "", 49.1))


if __name__ == "__main__":
    unittest.main()
