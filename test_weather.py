"""Проверки погоды без запросов к внешнему API."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram.constants import ChatType

import bot


FORECAST = {
    "current": {"temperature_2m": 12.5, "weather_code": 3, "precipitation": 0.2},
    "daily": {
        "temperature_2m_min": [8.0],
        "temperature_2m_max": [17.0],
        "precipitation_probability_max": [60],
        "weather_code": [61],
    },
}


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "tasks.db"
        bot.init_database(self.path)
        bot.save_location(self.path, 11, {
            "name": "Москва", "country": "Россия", "latitude": 55.75, "longitude": 37.62,
        })

    async def test_forecast_and_city_are_saved(self):
        location = bot.get_location(self.path, 11)
        self.assertEqual((location["city_name"], location["latitude"]), ("Москва", 55.75))
        with patch.object(bot, "fetch_json", new=AsyncMock(return_value=FORECAST)) as fetch:
            result = await bot.weather_text(location)
        self.assertIn("+12.5 °C, пасмурно", result)
        self.assertIn("осадки 0.2 мм", result)
        self.assertIn("вероятность осадков до 60%", result)
        self.assertEqual(fetch.call_args.args[0], bot.FORECAST_URL)
        self.assertEqual(fetch.call_args.args[1]["timezone"], "auto")

    async def test_today_shows_weather_when_there_are_no_tasks(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
            effective_user=SimpleNamespace(id=11),
            effective_message=message,
        )
        context = SimpleNamespace(application=SimpleNamespace(bot_data={
            "db_path": self.path, "timezone": ZoneInfo("Europe/Moscow"),
        }))
        with patch.object(bot, "fetch_json", new=AsyncMock(return_value=FORECAST)):
            await bot.show_tasks(update, context, today=True)
        self.assertEqual(message.reply_text.await_count, 2)
        self.assertIn("🌤 Москва", message.reply_text.await_args_list[0].args[0])
        self.assertEqual(message.reply_text.await_args_list[1].args[0], "На сегодня задач нет.")

    async def test_bad_weather_response_has_friendly_error(self):
        with patch.object(bot, "fetch_json", new=AsyncMock(return_value={"current": {}})):
            with self.assertRaises(bot.WeatherError):
                await bot.weather_text(bot.get_location(self.path, 11))

    async def test_city_choice_saves_coordinates(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            user_data={}, application=SimpleNamespace(bot_data={"db_path": self.path}),
        )
        user = SimpleNamespace(id=11)
        chat = SimpleNamespace(type=ChatType.PRIVATE)
        update = SimpleNamespace(effective_message=message, effective_user=user, effective_chat=chat)
        cities = [
            {"name": "Париж", "admin1": "", "country": "Франция", "latitude": 48.86, "longitude": 2.35},
            {"name": "Париж", "admin1": "Техас", "country": "США", "latitude": 33.66, "longitude": -95.56},
        ]
        with patch.object(bot, "search_cities", new=AsyncMock(return_value=cities)):
            await bot.select_city(update, context, "Париж")
        keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        callback_data = keyboard.inline_keyboard[1][0].callback_data
        query = SimpleNamespace(data=callback_data, message=message, answer=AsyncMock())
        update.callback_query = query
        await bot.callbacks(update, context)
        self.assertEqual(bot.get_location(self.path, 11)["longitude"], -95.56)
        self.assertEqual(context.user_data, {})


if __name__ == "__main__":
    unittest.main()
