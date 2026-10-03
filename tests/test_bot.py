import tempfile
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from cryptography.fernet import Fernet

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Chat, ChatMemberAdministrator, ChatMemberMember, ChatMemberOwner, ForumTopic, Message, MessageEntity, MessageId, Update, User

import bot as app


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.next_id = 1000
        self.admins = {10, 20}
        self.fail_forward = False
        self.fail_user = None
        self.fail_emoji = False
        self.bot_admin = True
        self.owners = {60}
        self.forum = False
        self.manage_topics = True
        self.delete_messages = True
        self.fail_delete_topic = False
        self.missing_threads = set()

    async def close(self):
        pass

    async def stream_content(self, url, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name == "getMe":
            return User(id=bot.id, is_bot=True, first_name="Bot", username=f"test{bot.id}bot")
        if name == "getChat":
            return Chat(id=method.chat_id, type="supergroup", is_forum=self.forum)
        if name == "createForumTopic":
            self.next_id += 1
            return ForumTopic(message_thread_id=self.next_id, name=method.name, icon_color=0x6FB9F0)
        if name in ("sendMessage", "forwardMessage", "copyMessage", "reopenForumTopic", "deleteForumTopic") and getattr(method, "message_thread_id", None) in self.missing_threads:
            raise TelegramBadRequest(method=method, message="message thread not found")
        if name == "deleteForumTopic" and self.fail_delete_topic:
            raise TelegramForbiddenError(method=method, message="No permissions")
        if name == "getChatMember":
            user = User(id=method.user_id, is_bot=method.user_id == bot.id, first_name="Staff")
            if method.user_id in self.owners:
                return ChatMemberOwner(user=user, is_anonymous=False)
            if (method.user_id not in self.admins and method.user_id != bot.id) or (method.user_id == bot.id and not self.bot_admin):
                return ChatMemberMember(user=user)
            return ChatMemberAdministrator(user=user, can_be_edited=False, is_anonymous=False,
                can_manage_chat=True, can_delete_messages=self.delete_messages, can_manage_video_chats=True,
                can_restrict_members=True, can_promote_members=True, can_change_info=True,
                can_invite_users=True, can_post_stories=True, can_edit_stories=True, can_delete_stories=True,
                can_send_welcome_messages=True, can_manage_topics=self.manage_topics)
        if name == "forwardMessage" and self.fail_forward:
            raise TelegramBadRequest(method=method, message="Cannot forward")
        if name in ("sendMessage", "copyMessage") and method.chat_id == self.fail_user:
            raise TelegramForbiddenError(method=method, message="Blocked")
        if name == "sendMessage" and self.fail_emoji and "tg-emoji" in method.text:
            raise TelegramBadRequest(method=method, message="Emoji unavailable")
        self.next_id += 1
        if name == "copyMessage":
            return MessageId(message_id=self.next_id)
        if name in ("sendMessage", "forwardMessage"):
            return Message(message_id=self.next_id, date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private" if method.chat_id > 0 else "supergroup"),
                from_user=User(id=bot.id, is_bot=True, first_name="Bot"), text=getattr(method, "text", None),
                message_thread_id=getattr(method, "message_thread_id", None))
        return True


class SupportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "support.sqlite3"
        self.store = app.Store(self.path)
        self.store.set_group(-100)
        self.session = TelegramSession()
        self.bot = Bot("99999:" + "a" * 35, session=self.session)
        self.cipher = Fernet(Fernet.generate_key())
        self.dp = Dispatcher()
        self.dp.include_router(app.create_router(self.store, 10, self.cipher))
        self.update_id = 0

    async def asyncTearDown(self):
        self.store.db.close()
        await self.bot.session.close()
        self.temp.cleanup()

    def message(self, user_id, text=None, chat_id=-100, message_id=1, reply_id=None, **kwargs):
        reply = kwargs.pop("reply_to_message", None)
        if reply_id:
            reply = Message(message_id=reply_id, date=datetime.now(timezone.utc),
                chat=Chat(id=chat_id, type="supergroup"), from_user=User(id=self.bot.id, is_bot=True, first_name="Bot"))
        return Message(message_id=message_id, date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            from_user=User(id=user_id, is_bot=False, first_name="User"),
            text=text, reply_to_message=reply, **kwargs)

    async def send(self, message):
        self.update_id += 1
        with patch.object(app.logging, "error") as errors:
            await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))
            errors.assert_not_called()

    def sent(self, chat_id):
        return [call for call in self.session.calls if call.__api_method__ == "sendMessage" and call.chat_id == chat_id]

    async def receive(self, user_id=30, message_id=5):
        await self.send(self.message(user_id, "Мой вопрос", user_id, message_id))
        return self.store.db.execute("SELECT message_id FROM relays WHERE user_id = ? AND source_id = ? ORDER BY message_id DESC",
                                     (user_id, message_id)).fetchone()[0]

    async def test_reply_goes_to_selected_user_with_signature_and_entities(self):
        first = await self.receive(30, 5)
        await self.receive(40, 6)
        await self.send(self.message(10, "/nickname 🌙Nightmare"))
        await self.send(self.message(10, "/role Владелец"))
        await self.send(self.message(10, "Ответ", message_id=10, reply_id=first,
            entities=[MessageEntity(type="italic", offset=0, length=5)]))
        answer = self.sent(30)[-1]
        self.assertEqual(answer.text, "🌙Nightmare [Владелец]\n\nОтвет")
        self.assertEqual(answer.entities[1].offset, app.utf16_length("🌙Nightmare [Владелец]\n\n"))
        self.assertEqual(answer.reply_parameters.message_id, 5)
        self.assertEqual(self.sent(40), [])

    async def test_ban_blocks_messages_and_answers_and_unban_restores(self):
        target = await self.receive()
        await self.send(self.message(10, "/banuser", reply_id=target))
        self.assertTrue(self.store.banned(30))
        count = self.store.db.execute("SELECT COUNT(*) FROM relays").fetchone()[0]
        await self.send(self.message(30, "Ещё вопрос", 30, 20))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM relays").fetchone()[0], count)
        await self.send(self.message(10, "/nickname Admin"))
        await self.send(self.message(10, "Ответ", reply_id=target))
        self.assertNotIn("Admin\n\nОтвет", [call.text for call in self.sent(30)])
        await self.send(self.message(10, "/unbanuser", reply_id=target))
        self.assertFalse(self.store.banned(30))
        await self.send(self.message(30, "Новый вопрос", 30, 21))
        self.assertGreater(self.store.db.execute("SELECT COUNT(*) FROM relays").fetchone()[0], count)

    async def test_member_cannot_claim_owner_or_curator_or_ban(self):
        target = await self.receive()
        await self.send(self.message(50, "/role Владелец"))
        await self.send(self.message(50, "/role Куратор"))
        await self.send(self.message(50, "/banuser", reply_id=target))
        self.assertIsNone(self.store.profile(50)["role"])
        self.assertFalse(self.store.banned(30))

    async def test_member_can_reply_and_missing_nickname_prevents_delivery(self):
        target = await self.receive()
        await self.send(self.message(50, "Ответ", reply_id=target))
        self.assertEqual(self.sent(30), [])
        await self.send(self.message(50, "/nickname Helper"))
        await self.send(self.message(50, "Ответ", reply_id=target))
        self.assertEqual(self.sent(30)[-1].text, "Helper\n\nОтвет")

    async def test_wrong_group_and_unrelated_replies_cannot_send_or_ban(self):
        target = await self.receive()
        await self.send(self.message(10, "/nickname Admin"))
        await self.send(self.message(10, "Ответ", chat_id=-200, reply_id=target))
        await self.send(self.message(10, "Ответ", reply_id=999))
        await self.send(self.message(10, "/banuser", reply_id=999))
        self.assertEqual(self.sent(30), [])
        self.assertFalse(self.store.banned(30))

    async def test_text_persistence_and_reply_after_restart(self):
        target = await self.receive()
        self.store.set_profile(10, "nickname", "Admin")
        self.store.set_ban(40, True)
        self.store.db.close()
        self.store = app.Store(self.path)
        self.dp = Dispatcher()
        self.dp.include_router(app.create_router(self.store, 10))
        self.assertEqual(self.store.group_id, -100)
        self.assertTrue(self.store.banned(40))
        await self.send(self.message(10, "Ответ после перезапуска", reply_id=target))
        self.assertEqual(self.sent(30)[-1].text, "Admin\n\nОтвет после перезапуска")

    async def test_forward_fallback_and_duplicate_update(self):
        self.session.fail_forward = True
        await self.receive()
        self.assertTrue(any(call.__api_method__ == "copyMessage" for call in self.session.calls))
        count = len(self.session.calls)
        await self.send(self.message(30, "Мой вопрос", 30, 5))
        self.assertEqual(len(self.session.calls), count)

    async def test_owner_grants_curator_to_member_and_it_does_not_require_admin(self):
        target = await self.receive()
        await self.send(self.message(20, "/nickname Curator"))
        await self.send(self.message(20, "/role Куратор"))
        self.assertIsNone(self.store.profile(20)["role"])
        await self.send(self.message(10, "/role Куратор 20"))
        self.assertEqual(self.store.profile(20)["role"], "Куратор")
        self.session.admins.remove(20)
        await self.send(self.message(20, "Ответ", reply_id=target))
        self.assertEqual(self.sent(30)[-1].text, "Curator [Куратор]\n\nОтвет")

    async def test_photo_reply_keeps_caption_and_signature(self):
        target = await self.receive()
        self.store.set_profile(10, "nickname", "Admin")
        await self.send(self.message(10, message_id=10, reply_id=target, caption="Инструкция",
            photo=[{"file_id": "photo", "file_unique_id": "p", "width": 10, "height": 10}]))
        copy = [call for call in self.session.calls if call.__api_method__ == "copyMessage"][-1]
        self.assertEqual(copy.chat_id, 30)
        self.assertEqual(copy.caption, "Admin\n\nИнструкция")

    async def test_long_text_and_sticker_have_separate_signature(self):
        target = await self.receive()
        self.store.set_profile(10, "nickname", "Admin")
        await self.send(self.message(10, "Я" * 4096, message_id=11, reply_id=target))
        await self.send(self.message(10, message_id=12, reply_id=target,
            sticker={"file_id": "s", "file_unique_id": "s", "type": "regular", "width": 10,
                     "height": 10, "is_animated": False, "is_video": False}))
        copies = [call for call in self.session.calls if call.__api_method__ == "copyMessage"]
        self.assertEqual(len(copies), 2)
        self.assertEqual([call.text for call in self.sent(30)], ["Admin", "Admin"])

    async def test_failed_delivery_is_not_marked_successful(self):
        target = await self.receive()
        self.store.set_profile(10, "nickname", "Admin")
        self.session.fail_user = 30
        await self.send(self.message(10, "Ответ", message_id=15, reply_id=target))
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM relays WHERE message_id = 15").fetchone())
        self.assertIn("Ответ не доставлен", self.sent(-100)[-1].text)

    async def test_greeting_has_premium_emoji_and_fallback(self):
        await self.send(self.message(30, "/start", 30))
        self.assertIn("tg-emoji", self.sent(30)[0].text)
        self.session.fail_emoji = True
        await self.send(self.message(30, "/start", 30))
        fallback = next(message for message in reversed(self.sent(30)) if "nightmaredlcmedia" in message.text)
        self.assertNotIn("tg-emoji", fallback.text)

    async def test_group_binding_requires_owner(self):
        await self.send(self.message(20, "/setgroup", chat_id=-200))
        self.assertEqual(self.store.group_id, -100)
        await self.send(self.message(10, "/setgroup", chat_id=-200))
        self.assertEqual(self.store.group_id, -200)

    async def test_group_binding_requires_bot_admin(self):
        self.session.bot_admin = False
        await self.send(self.message(10, "/setgroup", chat_id=-200))
        self.assertEqual(self.store.group_id, -100)

    async def test_anonymous_staff_cannot_reply(self):
        target = await self.receive()
        self.store.set_profile(10, "nickname", "Admin")
        await self.send(self.message(10, "Ответ", reply_id=target,
                                     sender_chat=Chat(id=-100, type="supergroup")))
        self.assertEqual(self.sent(30), [])

    async def test_header_can_be_used_for_reply_and_ban(self):
        await self.receive()
        header = self.store.db.execute("SELECT MIN(message_id) FROM relays WHERE user_id = 30").fetchone()[0]
        self.store.set_profile(10, "nickname", "Admin")
        await self.send(self.message(10, "Ответ по карточке", reply_id=header))
        self.assertEqual(self.sent(30)[-1].text, "Admin\n\nОтвет по карточке")
        await self.send(self.message(10, "/banuser", reply_id=header))
        self.assertTrue(self.store.banned(30))

    async def test_unban_by_id_restores_deleted_thread(self):
        self.store.set_ban(30, True)
        await self.send(self.message(10, "/unbanuser 30"))
        self.assertFalse(self.store.banned(30))

    async def test_staff_followup_routes_but_cannot_be_used_for_ban(self):
        target = await self.receive()
        self.store.set_profile(10, "nickname", "Admin")
        await self.send(self.message(10, "Первый ответ", message_id=15, reply_id=target))
        await self.send(self.message(10, "Продолжение", message_id=16, reply_id=15))
        self.assertEqual(self.sent(30)[-1].text, "Admin\n\nПродолжение")
        await self.send(self.message(10, "/banuser", reply_id=15))
        self.assertFalse(self.store.banned(30))

    async def test_private_owner_inbox_and_reply_without_group(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM settings")
        target = await self.receive()
        self.assertEqual(self.store.db.execute("SELECT chat_id FROM relays LIMIT 1").fetchone()[0], 10)
        await self.send(self.message(10, "/nickname Owner", chat_id=10))
        await self.send(self.message(10, "/role Владелец", chat_id=10))
        await self.send(self.message(10, "Личный ответ", chat_id=10, reply_id=target))
        self.assertEqual(self.sent(30)[-1].text, "Owner [Владелец]\n\nЛичный ответ")

    async def test_group_start_connect_binds_group(self):
        await self.send(self.message(10, "/start connect", chat_id=-200))
        self.assertEqual(self.store.group_id, -200)

    async def test_group_owner_can_grant_roles_but_admin_cannot_grant_or_remove(self):
        await self.send(self.message(60, "/role Куратор 50"))
        self.assertEqual(self.store.profile(50)["role"], "Куратор")
        await self.send(self.message(20, "/role убрать 50"))
        self.assertEqual(self.store.profile(50)["role"], "Куратор")
        await self.send(self.message(60, "/role убрать 50"))
        self.assertIsNone(self.store.profile(50)["role"])

    async def test_role_grant_by_reply_and_group_owner_signature(self):
        employee = self.message(50, "Привет", message_id=25)
        await self.send(self.message(10, "/role Куратор", reply_to_message=employee))
        self.assertEqual(self.store.profile(50)["role"], "Куратор")
        await self.send(self.message(60, "/role Владелец"))
        await self.send(self.message(60, "/nickname GroupOwner"))
        target = await self.receive()
        await self.send(self.message(60, "Ответ", reply_id=target))
        self.assertEqual(self.sent(30)[-1].text, "GroupOwner [Владелец]\n\nОтвет")

    async def test_owner_role_cannot_be_given_to_regular_staff(self):
        await self.send(self.message(10, "/role Владелец 50"))
        self.assertIsNone(self.store.profile(50)["role"])

    async def test_forum_binding_requires_manage_topics_and_delete_messages(self):
        self.session.forum = True
        self.session.manage_topics = False
        await self.send(self.message(10, "/setgroup", chat_id=-200))
        self.assertEqual(self.store.group_id, -100)
        self.session.manage_topics = True
        self.session.delete_messages = False
        await self.send(self.message(10, "/setgroup", chat_id=-200))
        self.assertEqual(self.store.group_id, -100)
        self.session.delete_messages = True
        await self.send(self.message(10, "/setgroup", chat_id=-200))
        self.assertEqual(self.store.group_id, -200)
        self.assertTrue(self.store.topics_enabled)

    async def test_messages_share_user_topic_and_other_users_get_separate_topics(self):
        self.store.set_topics(True)
        await self.receive(30, 5)
        await self.receive(30, 6)
        await self.receive(40, 7)
        first = self.store.ticket(-100, user_id=30)
        second = self.store.ticket(-100, user_id=40)
        self.assertNotEqual(first["thread_id"], second["thread_id"])
        creates = [call for call in self.session.calls if call.__api_method__ == "createForumTopic"]
        self.assertEqual(len(creates), 2)
        self.assertTrue(all(call.name.startswith("Вопрос от пользователя · ") for call in creates))
        forwards = [call for call in self.session.calls if call.__api_method__ == "forwardMessage"]
        self.assertEqual([call.message_thread_id for call in forwards], [first["thread_id"], first["thread_id"], second["thread_id"]])
        self.assertTrue(all(call.reply_markup.inline_keyboard[0][0].callback_data.startswith("close:") for call in self.sent(-100)))

    async def test_closed_topic_is_deleted_with_relays_and_next_question_creates_new(self):
        self.store.set_topics(True)
        await self.receive()
        ticket = self.store.ticket(-100, user_id=30)
        await self.send(self.message(20, "/close", message_thread_id=ticket["thread_id"]))
        closed = self.store.ticket(-100, user_id=30)
        self.assertIsNotNone(closed["delete_at"])
        await app.cleanup_topics(self.bot, self.store, now=closed["delete_at"] + 1)
        self.assertIsNone(self.store.ticket(-100, user_id=30))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM relays").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT value FROM settings WHERE key = 'questions_count'").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM visitors").fetchone()[0], 1)
        await self.receive(30, 6)
        self.assertNotEqual(self.store.ticket(-100, user_id=30)["thread_id"], ticket["thread_id"])

    async def test_new_message_reopens_closed_topic_and_cancels_deletion(self):
        self.store.set_topics(True)
        await self.receive()
        ticket = self.store.ticket(-100, user_id=30)
        await self.send(self.message(20, "/close", message_thread_id=ticket["thread_id"]))
        await self.receive(30, 6)
        reopened = self.store.ticket(-100, user_id=30)
        self.assertEqual(reopened["thread_id"], ticket["thread_id"])
        self.assertIsNone(reopened["delete_at"])
        self.assertTrue(any(call.__api_method__ == "reopenForumTopic" for call in self.session.calls))

    async def test_idle_topic_deletion_and_delivery_extends_activity(self):
        self.store.set_topics(True)
        target = await self.receive()
        old = self.store.ticket(-100, user_id=30)
        later = old["activity"] + app.config.TOPIC_IDLE_HOURS * 3600 - 1
        self.store.set_profile(10, "nickname", "Admin")
        with patch("bot.time.time", return_value=later):
            await self.send(self.message(10, "Ответ", reply_id=target))
        await app.cleanup_topics(self.bot, self.store, now=old["activity"] + app.config.TOPIC_IDLE_HOURS * 3600 + 1)
        self.assertIsNotNone(self.store.ticket(-100, user_id=30))
        await app.cleanup_topics(self.bot, self.store, now=later + app.config.TOPIC_IDLE_HOURS * 3600 + 1)
        self.assertIsNone(self.store.ticket(-100, user_id=30))

    async def test_failed_topic_deletion_keeps_mapping_for_retry(self):
        self.store.set_topics(True)
        await self.receive()
        ticket = self.store.ticket(-100, user_id=30)
        self.session.fail_delete_topic = True
        now = ticket["activity"] + app.config.TOPIC_IDLE_HOURS * 3600 + 1
        with patch("bot.logging.warning"):
            await app.cleanup_topics(self.bot, self.store, now=now)
        self.assertIsNotNone(self.store.ticket(-100, user_id=30))
        self.assertGreater(self.store.db.execute("SELECT COUNT(*) FROM relays").fetchone()[0], 0)
        self.session.fail_delete_topic = False
        await app.cleanup_topics(self.bot, self.store, now=now)
        self.assertIsNone(self.store.ticket(-100, user_id=30))

    async def test_manually_deleted_topic_is_recreated_on_new_question(self):
        self.store.set_topics(True)
        await self.receive()
        ticket = self.store.ticket(-100, user_id=30)
        self.session.missing_threads.add(ticket["thread_id"])
        await self.receive(30, 6)
        self.assertNotEqual(self.store.ticket(-100, user_id=30)["thread_id"], ticket["thread_id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM relays").fetchone()[0], 2)

    async def test_topic_mapping_survives_restart(self):
        self.store.set_topics(True)
        await self.receive()
        thread_id = self.store.ticket(-100, user_id=30)["thread_id"]
        self.store.db.close()
        self.store = app.Store(self.path)
        self.dp = Dispatcher()
        self.dp.include_router(app.create_router(self.store, 10))
        await self.receive(30, 6)
        self.assertEqual(self.store.ticket(-100, user_id=30)["thread_id"], thread_id)
        self.assertEqual(len([call for call in self.session.calls if call.__api_method__ == "createForumTopic"]), 1)

    async def test_close_button_closes_only_managed_topic(self):
        self.store.set_topics(True)
        await self.receive()
        ticket = self.store.ticket(-100, user_id=30)
        callback = CallbackQuery(id="close", from_user=User(id=20, is_bot=False, first_name="Staff"),
            chat_instance="test", data=f"close:{ticket['thread_id']}",
            message=self.message(20, "Карточка", message_thread_id=ticket["thread_id"]))
        with patch.object(app.logging, "error") as errors:
            await self.dp.feed_update(self.bot, Update(update_id=999, callback_query=callback))
            errors.assert_not_called()
        self.assertIsNotNone(self.store.ticket(-100, user_id=30)["delete_at"])

    async def test_manually_closed_and_reopened_topic_updates_deletion_deadline(self):
        self.store.set_topics(True)
        await self.receive()
        ticket = self.store.ticket(-100, user_id=30)
        await self.send(self.message(20, message_thread_id=ticket["thread_id"], forum_topic_closed={}))
        self.assertIsNotNone(self.store.ticket(-100, user_id=30)["delete_at"])
        await self.send(self.message(20, message_thread_id=ticket["thread_id"], forum_topic_reopened={}))
        self.assertIsNone(self.store.ticket(-100, user_id=30)["delete_at"])

    async def test_closed_topic_expired_before_cleanup_is_replaced_on_message(self):
        self.store.set_topics(True)
        await self.receive()
        ticket = self.store.ticket(-100, user_id=30)
        with self.store.db:
            self.store.db.execute("UPDATE tickets SET delete_at = 1")
        await self.receive(30, 6)
        self.assertNotEqual(self.store.ticket(-100, user_id=30)["thread_id"], ticket["thread_id"])
        self.assertTrue(any(call.__api_method__ == "deleteForumTopic" for call in self.session.calls))

    async def test_legacy_self_assigned_roles_are_removed_by_migration(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.execute("CREATE TABLE staff (user_id INTEGER PRIMARY KEY, nickname TEXT, role TEXT)")
        legacy.execute("INSERT INTO staff VALUES (20, 'Admin', 'Куратор')")
        legacy.commit()
        legacy.close()
        migrated = app.Store(path)
        try:
            self.assertEqual(migrated.profile(20)["nickname"], "Admin")
            self.assertIsNone(migrated.profile(20)["role"])
            migrated.set_role(20, "Куратор", 10)
        finally:
            migrated.db.close()
        migrated = app.Store(path)
        try:
            self.assertEqual(migrated.profile(20)["role"], "Куратор")
        finally:
            migrated.db.close()

    async def prepare_ai(self):
        target = await self.receive()
        self.store.set_profile(10, "nickname", "Helper")
        question = self.message(self.bot.id, "Как решить проблему?", message_id=target)
        with patch("bot.generate_ai_answer", new=AsyncMock(return_value="Попробуйте перезапустить игру.")) as generate:
            await self.send(self.message(10, "/ai ответь коротко", reply_to_message=question))
            generate.assert_awaited_once_with("Как решить проблему?", "ответь коротко", settings=self.store.ai_options(self.cipher))
        return self.store.db.execute("SELECT * FROM ai_drafts").fetchone()

    async def ai_click(self, draft, actor=10, action="send"):
        callback = CallbackQuery(id=f"{action}-{actor}", from_user=User(id=actor, is_bot=False, first_name="Staff"),
            chat_instance="test", data=f"ai_{action}:{draft['id']}",
            message=self.message(self.bot.id, "Черновик", message_id=self.session.next_id))
        with patch.object(app.logging, "error") as errors:
            await self.dp.feed_update(self.bot, Update(update_id=999, callback_query=callback))
            errors.assert_not_called()

    async def test_ai_draft_requires_click_and_is_sent_once_with_staff_signature(self):
        draft = await self.prepare_ai()
        self.assertEqual(self.sent(30), [])
        await self.ai_click(draft)
        self.assertEqual(self.sent(30)[-1].text, "Helper\n\nПопробуйте перезапустить игру.")
        self.assertIsNone(self.store.db.execute("SELECT * FROM ai_drafts").fetchone())
        count = len(self.sent(30))
        await self.ai_click(draft)
        self.assertEqual(len(self.sent(30)), count)

    async def test_ai_draft_cannot_be_sent_by_another_employee(self):
        draft = await self.prepare_ai()
        await self.ai_click(draft, actor=20)
        self.assertEqual(self.sent(30), [])
        self.assertIsNotNone(self.store.db.execute("SELECT * FROM ai_drafts").fetchone())

    async def test_cancel_and_expiry_prevent_ai_delivery(self):
        draft = await self.prepare_ai()
        await self.ai_click(draft, action="cancel")
        self.assertEqual(self.sent(30), [])
        self.assertIsNone(self.store.db.execute("SELECT * FROM ai_drafts").fetchone())
        draft = await self.prepare_ai()
        with self.store.db:
            self.store.db.execute("UPDATE ai_drafts SET created_at = 1")
        await self.ai_click(draft)
        self.assertEqual(self.sent(30), [])

    async def test_ban_prevents_sending_existing_ai_draft(self):
        draft = await self.prepare_ai()
        self.store.set_ban(30, True)
        await self.ai_click(draft)
        self.assertEqual(self.sent(30), [])

    async def test_ai_error_reports_to_staff_without_sending_to_player(self):
        target = await self.receive()
        self.store.set_profile(10, "nickname", "Helper")
        question = self.message(self.bot.id, "Вопрос", message_id=target)
        with patch("bot.generate_ai_answer", new=AsyncMock(side_effect=app.AIUnavailable("Лимит ИИ"))):
            await self.send(self.message(10, "/ai", reply_to_message=question))
        self.assertEqual(self.sent(30), [])
        self.assertEqual(self.sent(-100)[-1].text, "Лимит ИИ")

    def enable_auto_ai(self):
        self.store.set_ai("api_key", self.cipher.encrypt(b"test-secret").decode())
        self.store.set_ai("mode", "auto")

    async def test_auto_ai_answers_first_and_retains_context_without_forwarding(self):
        self.enable_auto_ai()
        with patch("bot.generate_ai_answer", new=AsyncMock(return_value="Уточните версию игры.")) as generate:
            await self.send(self.message(30, "Не работает игра", 30, 5))
            await self.send(self.message(30, "Последняя версия", 30, 6))
        self.assertEqual(len(self.sent(30)), 2)
        self.assertTrue(self.sent(30)[0].text.startswith("🤖 ИИ-помощник"))
        self.assertEqual(self.sent(30)[0].reply_markup.inline_keyboard[0][0].callback_data, "human")
        self.assertEqual(self.sent(-100), [])
        self.assertEqual(len(generate.await_args_list[1].kwargs["history"]), 2)
        self.assertEqual(self.store.dialogue(30)["source_id"], 6)

    async def test_human_request_hands_off_history_and_stops_auto_ai(self):
        self.enable_auto_ai()
        with patch("bot.generate_ai_answer", new=AsyncMock(return_value="Ответ ИИ")) as generate:
            await self.send(self.message(30, "Вопрос", 30, 5))
            await self.send(self.message(30, "Хочу живого оператора", 30, 6))
            await self.send(self.message(30, "Ещё вопрос сотруднику", 30, 7))
            self.assertEqual(generate.await_count, 1)
        self.assertTrue(self.store.dialogue(30)["human"])
        self.assertTrue(any("Переписка перед передачей" in (message.text or "") for message in self.sent(-100)))
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM relays WHERE user_id=30 AND source_id=7").fetchone())

    async def test_human_button_hands_off_last_question_and_is_idempotent(self):
        self.enable_auto_ai()
        with patch("bot.generate_ai_answer", new=AsyncMock(return_value="Ответ ИИ")):
            await self.send(self.message(30, "Вопрос", 30, 5))
        callback = CallbackQuery(id="human", from_user=User(id=30, is_bot=False, first_name="Player"),
            chat_instance="test", data="human", message=self.message(self.bot.id, "Ответ ИИ", 30, 6))
        await self.dp.feed_update(self.bot, Update(update_id=900, callback_query=callback))
        target = self.store.db.execute("SELECT message_id FROM relays WHERE user_id=30 AND incoming=1").fetchone()[0]
        self.store.set_profile(10, "nickname", "Helper")
        await self.send(self.message(10, "Ответ сотрудника", message_id=20, reply_id=target))
        count = len(self.sent(-100))
        await self.dp.feed_update(self.bot, Update(update_id=901, callback_query=callback))
        self.assertEqual(len(self.sent(-100)), count)
        self.assertTrue(self.store.dialogue(30)["human"])

    async def test_auto_api_failure_goes_to_staff_and_does_not_send_internal_error_to_player(self):
        self.enable_auto_ai()
        with patch("bot.generate_ai_answer", new=AsyncMock(side_effect=app.AIUnavailable("HTTP 503"))):
            await self.send(self.message(30, "Вопрос", 30, 5))
        self.assertTrue(self.store.dialogue(30)["human"])
        self.assertTrue(self.sent(-100))
        self.assertNotIn("HTTP 503", "\n".join(item.text or "" for item in self.sent(30)))

    async def test_duplicate_auto_update_is_not_answered_twice(self):
        self.enable_auto_ai()
        with patch("bot.generate_ai_answer", new=AsyncMock(return_value="Ответ")) as generate:
            await self.send(self.message(30, "Вопрос", 30, 5))
            await self.send(self.message(30, "Вопрос", 30, 5))
            generate.assert_awaited_once()

    async def test_new_command_restores_ai_after_human_handoff(self):
        self.enable_auto_ai()
        self.store.save_dialogue(30, True)
        await self.send(self.message(30, "/new", 30, 5))
        with patch("bot.generate_ai_answer", new=AsyncMock(return_value="Ответ")) as generate:
            await self.send(self.message(30, "Новый вопрос", 30, 6))
            generate.assert_awaited_once()
        self.assertFalse(self.store.dialogue(30)["human"])

    async def test_draft_mode_keeps_questions_in_staff_inbox(self):
        self.enable_auto_ai()
        self.store.set_ai("mode", "draft")
        with patch("bot.generate_ai_answer", new=AsyncMock()) as generate:
            await self.receive()
            generate.assert_not_awaited()
        self.assertTrue(self.sent(-100))

    async def test_only_owners_can_change_ai_settings_and_key_is_encrypted(self):
        await self.send(self.message(20, "/aisettings mode off"))
        self.assertEqual(self.store.ai_options(self.cipher)["mode"], "auto")
        await self.send(self.message(60, "/aisettings mode draft"))
        self.assertEqual(self.store.ai_options(self.cipher)["mode"], "draft")
        await self.send(self.message(10, "/aisettings key secret-owner-key", 10))
        saved = self.store.db.execute("SELECT value FROM ai_settings WHERE key='api_key'").fetchone()[0]
        self.assertNotIn("secret-owner-key", saved)
        self.assertEqual(self.store.ai_options(self.cipher)["api_key"], "secret-owner-key")
        self.assertTrue(any(call.__api_method__ == "deleteMessage" for call in self.session.calls))

    async def test_key_in_group_is_not_saved_and_provider_switch_clears_key(self):
        await self.send(self.message(10, "/aisettings key group-secret"))
        self.assertEqual(self.store.ai_options(self.cipher)["api_key"], "")
        await self.send(self.message(10, "/aisettings key private-secret", 10))
        await self.send(self.message(10, "/aisettings provider deepseek", 10))
        options = self.store.ai_options(self.cipher)
        self.assertEqual(options["api_key"], "")
        self.assertEqual(options["base_url"], "https://api.deepseek.com")

    async def test_prompt_and_facts_are_saved_and_applied_without_restart(self):
        await self.send(self.message(10, "/aisettings prompt Отвечай одним предложением.", 10))
        await self.send(self.message(10, "/aisettings facts Правило проекта", 10))
        options = self.store.ai_options(self.cipher)
        self.assertEqual(options["prompt"], "Отвечай одним предложением.")
        self.assertEqual(options["knowledge"], "Правило проекта")

    async def test_ai_key_and_handoff_survive_restart_and_old_history_expires(self):
        await self.send(self.message(10, "/aisettings key persisted-secret", 10))
        self.store.save_dialogue(30, True, 5, [{"role": "user", "content": "Вопрос"}])
        restored = app.Store(self.path)
        try:
            self.assertEqual(restored.ai_options(self.cipher)["api_key"], "persisted-secret")
            self.assertTrue(restored.dialogue(30)["human"])
            with restored.db:
                restored.db.execute("UPDATE ai_dialogues SET updated_at = 1")
            self.assertEqual(restored.dialogue(30)["history"], [])
            self.assertTrue(restored.dialogue(30)["human"])
        finally:
            restored.db.close()

    async def test_off_mode_prevents_drafting_and_regular_member_cannot_set_private_key(self):
        await self.send(self.message(30, "/aisettings key unauthorized-secret", 30))
        self.assertEqual(self.store.ai_options(self.cipher)["api_key"], "")
        target = await self.receive()
        self.store.set_ai("mode", "off")
        with patch("bot.generate_ai_answer", new=AsyncMock()) as generate:
            await self.send(self.message(10, "/ai", reply_id=target))
            generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
