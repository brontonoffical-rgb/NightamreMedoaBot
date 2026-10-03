import asyncio
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from manager import BotManager, TokenFilter, acquire_lock, configure_main, create_manager_router, extract_token
from test_bot import TelegramSession


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.session = TelegramSession()
        self.bot = Bot("99999:" + "a" * 35, session=self.session)
        self.child_sessions = []
        self.manager = BotManager(self.bot, self.directory, self.factory)
        self.manager.poll = AsyncMock(side_effect=self.idle)
        self.dp = Dispatcher()
        self.dp.include_router(create_manager_router(self.manager))
        self.counter = 0

    def factory(self, token):
        session = TelegramSession()
        self.child_sessions.append(session)
        return Bot(token, session=session)

    async def idle(self, bot, store, owner_id):
        await asyncio.Event().wait()

    async def asyncTearDown(self):
        await self.manager.close()
        await self.bot.session.close()
        self.temp.cleanup()

    def token(self, bot_id=11111):
        return f"{bot_id}:" + "a" * 35

    def message(self, user_id, text, chat_id=None):
        return Message(message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id or user_id, type="private" if (chat_id or user_id) > 0 else "supergroup"),
            from_user=User(id=user_id, is_bot=False, first_name="Owner"), text=text)

    async def send(self, message=None, callback=None):
        self.counter += 1
        with patch("manager.logging.error") as errors:
            await self.dp.feed_update(self.bot, Update(update_id=self.counter, message=message, callback_query=callback))
            errors.assert_not_called()

    async def test_addbot_forwarded_token_connects_and_is_deleted(self):
        await self.send(self.message(10, "/addbot"))
        self.assertIn(10, self.manager.waiting)
        await self.send(self.message(10, "Done! Your API token:\n" + self.token()))
        row = self.manager.owned(11111, 10)
        self.assertIsNotNone(row)
        self.assertNotIn(self.token().encode(), row["token"])
        self.assertEqual(self.manager.cipher.decrypt(row["token"]).decode(), self.token())
        self.assertNotIn(10, self.manager.waiting)
        self.assertTrue(any(call.__api_method__ == "deleteMessage" for call in self.session.calls))
        texts = [call.text for call in self.session.calls if call.__api_method__ == "sendMessage"]
        self.assertTrue(any("подключён" in text for text in texts))
        self.assertFalse(any(self.token() in text for text in texts))

    async def test_startup_retries_transient_telegram_network_error(self):
        me = User(id=self.bot.id, is_bot=True, first_name="Bot", username="servicebot")
        error = TelegramNetworkError(method=GetMe(), message="Temporary network failure")
        with patch.object(self.bot, "get_me", new=AsyncMock(side_effect=[error, me])) as request:
            with patch("manager.asyncio.sleep", new=AsyncMock()) as sleep:
                with patch("manager.logging.warning"):
                    self.assertEqual(await configure_main(self.bot), me)
                self.assertEqual(request.await_count, 2)
                sleep.assert_awaited_once_with(2)

    async def test_main_bot_token_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "главного бота"):
            await self.manager.connect(self.token(99999), 10)
        self.assertEqual(self.manager.workers, {})

    async def test_existing_bot_cannot_be_taken_by_another_owner(self):
        await self.manager.connect(self.token(), 10)
        with self.assertRaisesRegex(ValueError, "другим пользователем"):
            await self.manager.connect(self.token(), 20)
        self.assertIsNotNone(self.manager.owned(11111, 10))
        self.assertIsNone(self.manager.owned(11111, 20))

    async def test_two_bots_keep_inboxes_profiles_and_bans_separate(self):
        await self.manager.connect(self.token(11111), 10)
        await self.manager.connect(self.token(22222), 20)
        first = self.manager.workers[11111][2]
        second = self.manager.workers[22222][2]
        first.set_group(-100)
        first.set_profile(50, "nickname", "First")
        first.set_ban(30, True)
        first.link(-100, 1, 30, 5)
        second.set_group(-200)
        self.assertEqual(second.group_id, -200)
        self.assertIsNone(second.profile(50)["nickname"])
        self.assertFalse(second.banned(30))
        self.assertIsNone(second.target(Message(message_id=2, date=datetime.now(timezone.utc),
            chat=Chat(id=-100, type="supergroup"), reply_to_message=Message(message_id=1,
                date=datetime.now(timezone.utc), chat=Chat(id=-100, type="supergroup")))))

    async def test_disconnect_removes_token_and_stops_worker_but_keeps_settings(self):
        await self.manager.connect(self.token(), 10)
        self.manager.workers[11111][2].set_group(-100)
        old_task = self.manager.workers[11111][1]
        self.assertFalse(await self.manager.disconnect(11111, 20))
        self.assertFalse(old_task.done())
        self.assertTrue(await self.manager.disconnect(11111, 10))
        self.assertTrue(old_task.done())
        self.assertNotIn(11111, self.manager.workers)
        self.assertIsNone(self.manager.owned(11111, 10)["token"])
        await self.manager.connect(self.token(), 10)
        self.assertEqual(self.manager.workers[11111][2].group_id, -100)

    async def test_rotating_token_replaces_worker_without_duplicate_polling(self):
        await self.manager.connect(self.token(), 10)
        old_task = self.manager.workers[11111][1]
        new_token = "11111:" + "b" * 35
        await self.manager.connect(new_token, 10)
        self.assertTrue(old_task.done())
        self.assertEqual(len(self.manager.workers), 1)
        self.assertEqual(self.manager.cipher.decrypt(self.manager.owned(11111, 10)["token"]).decode(), new_token)

    async def test_restore_after_restart_restores_only_active_bots(self):
        await self.manager.connect(self.token(), 10)
        await self.manager.connect(self.token(22222), 20)
        await self.manager.disconnect(22222, 20)
        self.manager.workers[11111][2].set_profile(10, "nickname", "Owner")
        await self.manager.close()
        self.manager = BotManager(self.bot, self.directory, self.factory)
        self.manager.poll = AsyncMock(side_effect=self.idle)
        await self.manager.restore()
        self.assertEqual(set(self.manager.workers), {11111})
        self.assertEqual(self.manager.workers[11111][2].profile(10)["nickname"], "Owner")

    async def test_foreign_callback_cannot_disconnect_or_view_card(self):
        await self.manager.connect(self.token(), 10)
        for action in ("bot", "disconnect"):
            callback = CallbackQuery(id=action, from_user=User(id=20, is_bot=False, first_name="Other"),
                chat_instance="test", data=f"{action}:11111", message=self.message(20, "Menu"))
            await self.send(callback=callback)
        self.assertIn(11111, self.manager.workers)
        alerts = [call for call in self.session.calls if call.__api_method__ == "answerCallbackQuery"]
        self.assertTrue(all(call.show_alert for call in alerts))

    async def test_token_in_group_is_ignored(self):
        await self.send(self.message(10, self.token(), chat_id=-100))
        self.assertEqual(self.manager.workers, {})

    async def test_bot_list_only_shows_own_bots_and_cancel_clears_state(self):
        await self.manager.connect(self.token(), 10)
        await self.manager.connect(self.token(22222), 20)
        _, keyboard = self.manager.bot_list(10)
        data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("bot:11111", data)
        self.assertNotIn("bot:22222", data)
        await self.send(self.message(10, "/addbot"))
        await self.send(self.message(10, "/cancel"))
        self.assertNotIn(10, self.manager.waiting)

    async def test_card_counts_questions_without_double_counting_header(self):
        await self.manager.connect(self.token(), 10)
        store = self.manager.workers[11111][2]
        store.link(-100, 1, 30, 5)
        store.link(-100, 2, 30, 5)
        store.link(-100, 3, 30, 5, False)
        store.record_received(30)
        text, _ = self.manager.card(11111, 10)
        self.assertIn("Вопросов: 1", text)
        self.assertIn("Пользователей: 1", text)


class TokenTests(unittest.TestCase):
    def test_second_instance_is_blocked_until_first_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            first = acquire_lock(directory)
            try:
                with self.assertRaisesRegex(ValueError, "уже запущен"):
                    acquire_lock(directory)
            finally:
                first.close()
            second = acquire_lock(directory)
            second.close()

    def test_token_from_botfather_message(self):
        token = "12345:" + "a" * 35
        self.assertEqual(extract_token("Token:\n" + token + "\nKeep it safe."), token)
        self.assertIsNone(extract_token("Привет"))

    def test_log_tokens_are_redacted(self):
        token = "12345:" + "a" * 35
        record = logging.LogRecord("test", logging.ERROR, "", 1, "Failed %s", (token,), None)
        self.assertTrue(TokenFilter().filter(record))
        self.assertNotIn(token, record.getMessage())
        self.assertIn("[TOKEN]", record.getMessage())


if __name__ == "__main__":
    unittest.main()
