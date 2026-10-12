"""Проверки журнала, поиска и разделения данных пользователей."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram.constants import ChatType

import bot


FIXED_NOW = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("Europe/Moscow")


class HistorySearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "tasks.db"
        bot.init_database(self.path)
        self.message = SimpleNamespace(reply_text=AsyncMock(), text="/start")
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=11, type=ChatType.PRIVATE),
            effective_user=SimpleNamespace(
                id=11, username="tester", first_name="Тест", last_name=None,
            ),
            effective_message=self.message,
            message=self.message,
            callback_query=None,
        )
        self.context = SimpleNamespace(
            user_data={},
            application=SimpleNamespace(bot_data={"db_path": self.path, "timezone": TZ}),
        )

    async def test_task_actions_survive_delete_and_are_private(self):
        with patch.object(bot, "now_utc", return_value=FIXED_NOW):
            first = bot.add_task(self.path, 11, 11, "Купить молоко", "normal", None, 0)
            bot.add_task(self.path, 22, 22, "Чужая задача", "low", None, 0)
            self.assertEqual(first, 1)
            self.context.user_data["edit_id"] = bot.get_task(self.path, 11, 1)["id"]
            await bot.update_field(self.update, self.context, "title", "Купить хлеб")
            await bot.dispatch_action(self.update, self.context, "done", 1)
            task_id = bot.get_history(self.path, 11)[0]["task_id"]
            self.update.callback_query = SimpleNamespace(
                data=f"confirm:{task_id}", message=self.message, answer=AsyncMock(),
            )
            await bot.callbacks(self.update, self.context)
        self.assertIsNone(bot.get_task_by_id(self.path, 11, task_id))
        history = bot.get_history(self.path, 11)
        self.assertEqual([row["action"] for row in history],
                         ["deleted", "completed", "edited", "created"])
        self.assertEqual(history[0]["task_title"], "Купить хлеб")
        self.assertEqual((history[2]["old_value"], history[2]["new_value"]),
                         ("Купить молоко", "Купить хлеб"))
        self.assertEqual(len(bot.get_history(self.path, 22)), 1)
        self.assertEqual(bot.find_tasks(self.path, 11, bot.parse_search("статус=все"), TZ), [])
        self.assertEqual(bot.get_task(self.path, 22, 1)["title"], "Чужая задача")

    async def test_search_and_week_use_owner_status_and_local_date(self):
        with patch.object(bot, "now_utc", return_value=FIXED_NOW):
            bot.add_task(self.path, 11, 11, "Купить МОЛОКО", "normal", None, 0)
            due = bot.to_db(FIXED_NOW + timedelta(days=2))
            bot.add_task(self.path, 11, 11, "Позвонить", "high", due, 0)
            bot.add_task(self.path, 22, 22, "Купить молоко", "normal", None, 0)
            await bot.dispatch_action(self.update, self.context, "done", 1)
            criteria = bot.parse_search("молоко статус=выполнена дата=12.10.2026")
            found = bot.find_tasks(self.path, 11, criteria, TZ)
            self.assertEqual([row["title"] for row in found], ["Купить МОЛОКО"])
            due_found = bot.find_tasks(
                self.path, 11, bot.parse_search("дата=14.10.2026"), TZ,
            )
            self.assertEqual([row["title"] for row in due_found], ["Позвонить"])
            self.assertEqual(len(bot.recent_tasks(self.path, 11, TZ)), 2)
            with bot.database(self.path) as db:
                db.execute(
                    "UPDATE tasks SET created_at = ? WHERE user_id = ? AND title = ?",
                    (bot.to_db(FIXED_NOW - timedelta(days=8)), 11, "Позвонить"),
                )
            self.assertEqual([row["title"] for row in bot.recent_tasks(self.path, 11, TZ)],
                             ["Купить МОЛОКО"])

    async def test_user_profile_and_interaction_metadata(self):
        with patch.object(bot, "now_utc", return_value=FIXED_NOW):
            await bot.track_interaction(self.update, self.context)
            self.message.text = "Личный текст"
            await bot.track_interaction(self.update, self.context)
        with bot.database(self.path) as db:
            user = db.execute("SELECT * FROM users WHERE user_id = 11").fetchone()
            events = db.execute(
                "SELECT kind, name FROM interactions WHERE user_id = 11 ORDER BY id"
            ).fetchall()
        self.assertEqual((user["username"], user["chat_id"]), ("tester", 11))
        self.assertEqual([(row["kind"], row["name"]) for row in events],
                         [("command", "/start"), ("message", "text")])
        self.assertNotIn("Личный текст", str([tuple(row) for row in events]))

    async def test_invalid_search_filters(self):
        for query in ("статус=неизвестен", "дата=31.02.2026", "дата=1.2.2026"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                bot.parse_search(query)

    async def test_existing_tasks_survive_new_table_migration(self):
        bot.add_task(self.path, 11, 11, "Сохранённая задача", "normal", None, 0)
        with bot.database(self.path) as db:
            db.execute("DROP TABLE task_history")
            db.execute("DROP TABLE interactions")
            db.execute("DROP TABLE users")
        bot.init_database(self.path)
        self.assertEqual(bot.get_task(self.path, 11, 1)["title"], "Сохранённая задача")
        with bot.database(self.path) as db:
            user = db.execute("SELECT * FROM users WHERE user_id = 11").fetchone()
            history_table = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'task_history'"
            ).fetchone()
        self.assertEqual(user["chat_id"], 11)
        self.assertIsNotNone(history_table)

    async def test_search_pages_keep_the_same_filter(self):
        with patch.object(bot, "now_utc", return_value=FIXED_NOW):
            for number in range(11):
                bot.add_task(self.path, 11, 11, f"Покупка {number}", "normal", None, 0)
        await bot.run_search(self.update, self.context, "покупка статус=открыта")
        self.assertEqual(self.message.reply_text.await_count, 12)
        keyboard = self.message.reply_text.await_args.kwargs["reply_markup"]
        callback_data = keyboard.inline_keyboard[0][0].callback_data
        self.message.reply_text.reset_mock()
        self.update.callback_query = SimpleNamespace(
            data=callback_data, message=self.message, answer=AsyncMock(),
        )
        await bot.callbacks(self.update, self.context)
        self.assertIn("11–11 из 11", self.message.reply_text.await_args_list[0].args[0])
        self.assertEqual(self.message.reply_text.await_count, 3)


if __name__ == "__main__":
    unittest.main()
