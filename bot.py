import asyncio
import json
import logging
import re
import sqlite3
import time
from html import escape
from urllib.parse import urlsplit

if __name__ == "__main__":
    import os
    if os.name == "nt":
        import subprocess
        from pathlib import Path
        raise SystemExit(subprocess.call(["cmd.exe", "/c", str(Path(__file__).with_name("start.bat"))]))

import aiohttp
from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, MessageEntity, ReplyParameters

import config


class Store:
    def __init__(self, path):
        self.lock = asyncio.Lock()
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS staff (user_id INTEGER PRIMARY KEY, nickname TEXT, role TEXT, role_granted_by INTEGER);
            CREATE TABLE IF NOT EXISTS bans (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS relays (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, source_id INTEGER NOT NULL, incoming INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS tickets (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                activity REAL NOT NULL, delete_at REAL,
                PRIMARY KEY (chat_id, user_id), UNIQUE (chat_id, thread_id)
            );
            CREATE TABLE IF NOT EXISTS visitors (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS ai_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL, staff_id INTEGER NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_dialogues (
                user_id INTEGER PRIMARY KEY, human INTEGER NOT NULL DEFAULT 0,
                source_id INTEGER NOT NULL DEFAULT 0, history TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL
            );
            INSERT OR IGNORE INTO visitors SELECT DISTINCT user_id FROM relays WHERE incoming = 1;
            INSERT OR IGNORE INTO settings VALUES ('questions_count',
                (SELECT COUNT(*) FROM (SELECT DISTINCT user_id, source_id FROM relays WHERE incoming = 1)));
        """)
        if "role_granted_by" not in {row[1] for row in self.db.execute("PRAGMA table_info(staff)")}:
            with self.db:
                self.db.execute("ALTER TABLE staff ADD COLUMN role_granted_by INTEGER")
                self.db.execute("UPDATE staff SET role = NULL")

    @property
    def group_id(self):
        row = self.db.execute("SELECT value FROM settings WHERE key = 'group_id'").fetchone()
        return row[0] if row else None

    def set_group(self, chat_id):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('group_id', ?)", (chat_id,))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('topics_enabled', 0)")

    @property
    def topics_enabled(self):
        row = self.db.execute("SELECT value FROM settings WHERE key = 'topics_enabled'").fetchone()
        return bool(row and row[0])

    def set_topics(self, enabled):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('topics_enabled', ?)", (int(enabled),))

    def set_profile(self, user_id, field, value):
        if field != "nickname":
            raise ValueError("Unknown profile field")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO staff (user_id) VALUES (?)", (user_id,))
            self.db.execute(f"UPDATE staff SET {field} = ? WHERE user_id = ?", (value, user_id))

    def set_role(self, user_id, role, granted_by):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO staff (user_id) VALUES (?)", (user_id,))
            self.db.execute("UPDATE staff SET role = ?, role_granted_by = ? WHERE user_id = ?", (role, granted_by, user_id))

    def ticket(self, chat_id, user_id=None, thread_id=None):
        if user_id is not None:
            return self.db.execute("SELECT * FROM tickets WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)).fetchone()
        return self.db.execute("SELECT * FROM tickets WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id)).fetchone()

    def save_ticket(self, chat_id, user_id, thread_id):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tickets VALUES (?, ?, ?, ?, NULL)", (chat_id, user_id, thread_id, time.time()))

    def touch_ticket(self, chat_id, user_id):
        with self.db:
            self.db.execute("UPDATE tickets SET activity = ? WHERE chat_id = ? AND user_id = ?", (time.time(), chat_id, user_id))

    def forget_ticket(self, ticket):
        with self.db:
            self.db.execute("DELETE FROM ai_drafts WHERE chat_id = ? AND target_id IN (SELECT message_id FROM relays WHERE chat_id = ? AND user_id = ?)",
                            (ticket["chat_id"], ticket["chat_id"], ticket["user_id"]))
            self.db.execute("DELETE FROM tickets WHERE chat_id = ? AND thread_id = ?", (ticket["chat_id"], ticket["thread_id"]))
            self.db.execute("DELETE FROM relays WHERE chat_id = ? AND user_id = ?", (ticket["chat_id"], ticket["user_id"]))

    def record_received(self, user_id):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO visitors VALUES (?)", (user_id,))
            self.db.execute("UPDATE settings SET value = value + 1 WHERE key = 'questions_count'")

    def profile(self, user_id):
        row = self.db.execute("SELECT nickname, role FROM staff WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else {"nickname": None, "role": None}

    def banned(self, user_id):
        return self.db.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,)).fetchone() is not None

    def set_ban(self, user_id, banned):
        with self.db:
            if banned:
                self.db.execute("INSERT OR IGNORE INTO bans VALUES (?)", (user_id,))
            else:
                self.db.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))

    def link(self, chat_id, message_id, user_id, source_id, incoming=True):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO relays VALUES (?, ?, ?, ?, ?)",
                            (chat_id, message_id, user_id, source_id, int(incoming)))

    def target(self, message):
        if not message.reply_to_message:
            return None
        return self.db.execute("SELECT * FROM relays WHERE chat_id = ? AND message_id = ?",
                               (message.chat.id, message.reply_to_message.message_id)).fetchone()

    def ai_options(self, cipher=None):
        options = {"provider": config.AI_PROVIDER, "base_url": config.AI_BASE_URL, "model": config.AI_MODEL,
                   "mode": config.AI_MODE, "prompt": config.AI_PROMPT, "knowledge": config.AI_KNOWLEDGE,
                   "api_key": config.AI_API_KEY}
        saved = dict(self.db.execute("SELECT key, value FROM ai_settings"))
        encrypted = saved.pop("api_key", None)
        options.update(saved)
        if encrypted is not None:
            options["api_key"] = ""
        if encrypted and cipher:
            try:
                options["api_key"] = cipher.decrypt(encrypted.encode()).decode()
            except Exception:
                raise AIUnavailable("Не удалось прочитать ключ ИИ. Владелец должен заново задать /aisettings key в личке.") from None
        elif encrypted:
            options["api_key"] = ""
        return options

    def set_ai(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO ai_settings VALUES (?, ?)", (key, value))

    def dialogue(self, user_id):
        row = self.db.execute("SELECT * FROM ai_dialogues WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {"human": False, "source_id": 0, "history": []}
        return {"human": bool(row["human"]), "source_id": row["source_id"],
                "history": json.loads(row["history"]) if row["updated_at"] > time.time() - 3600 else []}

    def save_dialogue(self, user_id, human, source_id=0, history=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO ai_dialogues VALUES (?, ?, ?, ?, ?)",
                (user_id, int(human), source_id, json.dumps((history or [])[-6:], ensure_ascii=False), time.time()))


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def topic_missing(error):
    return any(text in str(error).lower() for text in ("message thread not found", "topic_deleted", "topic_id_invalid", "topic not found"))


async def delete_ticket(bot, store, ticket):
    try:
        await bot.delete_forum_topic(ticket["chat_id"], ticket["thread_id"])
    except TelegramBadRequest as error:
        if not topic_missing(error):
            raise
    store.forget_ticket(ticket)


async def cleanup_topics(bot, store, now=None):
    now = time.time() if now is None else now
    async with store.lock:
        with store.db:
            store.db.execute("DELETE FROM ai_drafts WHERE created_at <= ?", (now - 600,))
            store.db.execute("UPDATE ai_dialogues SET history = '[]' WHERE updated_at <= ? AND history != '[]'", (now - 3600,))
        due = store.db.execute("""SELECT * FROM tickets WHERE delete_at <= ?
            OR (delete_at IS NULL AND activity <= ?) LIMIT 50""", (now, now - config.TOPIC_IDLE_HOURS * 3600)).fetchall()
        for ticket in due:
            try:
                await delete_ticket(bot, store, ticket)
            except TelegramAPIError:
                logging.warning("Не удалось удалить тему %s в группе %s. Проверьте права бота.", ticket["thread_id"], ticket["chat_id"])


async def topic_cleanup_loop(bot, store):
    while True:
        await cleanup_topics(bot, store)
        await asyncio.sleep(60)


class AIUnavailable(Exception):
    pass


async def generate_ai_answer(question, instructions="", session=None, settings=None, history=None):
    settings = settings or {"api_key": config.AI_API_KEY, "base_url": config.AI_BASE_URL,
                           "model": config.AI_MODEL, "prompt": config.AI_PROMPT, "knowledge": config.AI_KNOWLEDGE}
    api_key = settings["api_key"].strip()
    if not api_key:
        raise AIUnavailable("ИИ не настроен: нужен личный API-ключ. Владелец может задать /aisettings key КЛЮЧ в личке этого бота.")
    if not settings["base_url"] or not settings["model"]:
        raise AIUnavailable("Выберите API и модель через /aisettings provider или /aisettings url и /aisettings model.")
    endpoint = settings["base_url"].rstrip("/") + "/chat/completions"
    owned_session = session is None
    if owned_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20, connect=5), trust_env=False)
    try:
        payload = {
            "model": settings["model"], "max_tokens": 600,
            "messages": [
                {"role": "system", "content": "Ты ИИ-помощник поддержки Nightmare. " + settings["prompt"] +
                 " Факты поддержки: " + settings["knowledge"]},
                *(history or [])[-6:],
                {"role": "user", "content": "Вопрос игрока:\n" + question[:8000] +
                 ("\n\nПожелание сотрудника:\n" + instructions[:2000] if instructions else "")}
            ]}
        if settings.get("provider") == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        for attempt in range(2):
            try:
                async with session.post(endpoint, json=payload, headers={"Authorization": "Bearer " + api_key}) as response:
                    status = response.status
                    if status >= 500 and attempt == 0:
                        await response.read()
                        await asyncio.sleep(1)
                        continue
                    if status == 401:
                        raise AIUnavailable("API-ключ не принят (HTTP 401). Замените ключ через /aisettings key в личке бота.")
                    if status == 403:
                        raise AIUnavailable("API отказал в доступе (HTTP 403). Проверьте регион и разрешения ключа или выберите другой провайдер.")
                    if status == 429:
                        raise AIUnavailable("Достигнут лимит API (HTTP 429). Попробуйте позже или смените модель через /aisettings model.")
                    if status == 402:
                        raise AIUnavailable("API требует баланс (HTTP 402). Проверьте баланс и тариф выбранной модели у провайдера.")
                    if status == 404:
                        raise AIUnavailable("API или модель не найдены (HTTP 404). Проверьте /aisettings provider и /aisettings model.")
                    if status >= 400:
                        raise AIUnavailable(f"Провайдер ИИ вернул HTTP {status}. Проверьте настройки через /aisettings; используйте /aitest для проверки.")
                    result = await response.json(content_type=None)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt:
                    raise
                await asyncio.sleep(1)
        answer = result["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or not answer.strip():
            raise AIUnavailable("ИИ вернул пустой ответ. Попробуйте ещё раз.")
        answer = answer.strip()[:3500]
        while utf16_length(answer) > 3500:
            answer = answer[:-1]
        return answer
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise AIUnavailable("Нет связи с API ИИ после двух попыток. Проверьте адрес провайдера; поддержка продолжает работать без ИИ.") from None
    except (KeyError, IndexError, TypeError, ValueError):
        raise AIUnavailable("Сервис ИИ вернул некорректный ответ. Попробуйте позже.") from None
    finally:
        if owned_session:
            await session.close()


def signed_text(signature, text, entities):
    prefix = signature + "\n\n"
    shifted = [entity.model_copy(update={"offset": entity.offset + utf16_length(prefix)})
               for entity in entities or []]
    return prefix + text, [MessageEntity(type="bold", offset=0, length=utf16_length(signature)), *shifted]


def greeting(premium=True):
    def emoji(emoji_id, fallback):
        if premium:
            return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
        return fallback
    return (
        f'{emoji(config.EMOJI_HELLO, "👋")} <b>Привет Привет наш друг!</b>\n\n'
        f'{emoji(config.EMOJI_QUESTION, "📄")} <b>👻Для получения бесплатной бета версии пришли нам ссылку на свой тикток/ютуб и скриншот с открытим тиктоком или ютубом + ботом и незабудь про свой юз, '
    )


def group_commands():
    return [BotCommand(command=name, description=description) for name, description in (
        ("help", "Как отвечать пользователям"), ("nickname", "Установить свой ник"),
        ("role", "Владелец, Куратор или убрать"), ("banuser", "Заблокировать автора сообщения"),
        ("unbanuser", "Разблокировать автора сообщения"), ("setgroup", "Привязать эту группу"),
        ("close", "Закрыть обращение в этой теме"),
        ("ai", "Подготовить ИИ-ответ на вопрос"),
        ("aisettings", "Настройки ИИ для владельца"), ("aitest", "Проверить подключение ИИ"),
        ("airesume", "Вернуть обращение ИИ"),
        ("myid", "Показать свой Telegram ID"))]


def create_router(store, owner_id, ai_cipher=None):
    router = Router()

    async def serialize(handler, event, data):
        async with store.lock:
            return await handler(event, data)

    router.message.outer_middleware(serialize)
    router.callback_query.outer_middleware(serialize)

    async def staff_allowed(message):
        if message.chat.id != store.group_id and not (message.chat.type == "private" and message.chat.id == owner_id):
            return False
        if message.sender_chat:
            await message.reply("Выключите анонимность администратора и отправьте сообщение от своего аккаунта.")
            return False
        return bool(message.from_user and not message.from_user.is_bot)

    async def admin_allowed(message, bot):
        if message.from_user.id == owner_id:
            return True
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)

    async def is_owner(user_id, bot):
        if user_id == owner_id:
            return True
        if not store.group_id:
            return False
        return (await bot.get_chat_member(store.group_id, user_id)).status == ChatMemberStatus.CREATOR

    @router.message(Command("aisettings"))
    async def ai_settings(message: Message, bot: Bot, command: CommandObject):
        field, _, value = (command.args or "").strip().partition(" ")
        value = value.strip()
        if field == "key" and message.chat.type != "private":
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            await message.answer("Ключ вводится только в личке этого бота. Если отправили его в группу, замените ключ у провайдера.")
            return
        if not message.from_user or message.sender_chat or not await is_owner(message.from_user.id, bot):
            await message.reply("Настройки ИИ может менять только владелец бота или группы.")
            return
        if not field:
            options = store.ai_options(ai_cipher)
            await message.reply(f"⚙️ ИИ: {options['provider']}\nМодель: {options['model']}\nРежим: {options['mode']}\n"
                f"Ключ: {'задан' if options['api_key'] else 'не задан'}\nАдрес: {options['base_url']}\n\n"
                "/aisettings provider deepseek — платный API DeepSeek\n"
                "/aisettings provider pollinations — Pollinations с ключом\n"
                "/aisettings provider openrouter — OpenRouter с лимитами тарифа\n"
                "/aisettings key КЛЮЧ — только в личке\n/aisettings model ID_МОДЕЛИ\n"
                "/aisettings mode auto — ИИ отвечает первым\n/aisettings mode draft — только черновики для сотрудников\n"
                "/aisettings mode off — ответы сотрудников\n/aisettings prompt ИНСТРУКЦИЯ\n"
                "/aisettings facts ИНФОРМАЦИЯ_О_ПРОЕКТЕ\n/aisettings url HTTPS_АДРЕС_API — свой API\n"
                "/aitest — проверить ответ\n/aisettings show — показать инструкции")
            return
        if field == "provider":
            providers = {"openrouter": ("https://openrouter.ai/api/v1", "openrouter/free"),
                         "deepseek": ("https://api.deepseek.com", "deepseek-flash"),
                         "pollinations": ("https://gen.pollinations.ai/v1", "openai/gpt-5.4-nano")}
            if value not in providers:
                await message.reply("Доступны openrouter, deepseek и pollinations. Для другого API используйте /aisettings url.")
                return
            base_url, model = providers[value]
            if store.ai_options(ai_cipher)["provider"] != value:
                store.set_ai("api_key", "")
            for key, setting in (("provider", value), ("base_url", base_url), ("model", model)):
                store.set_ai(key, setting)
        elif field == "key":
            if not ai_cipher:
                await message.reply("Не найден ключ шифрования сервиса. Запустите бота через start.bat.")
                return
            if not value or len(value) > 512 or any(character.isspace() for character in value):
                await message.reply("Используйте /aisettings key КЛЮЧ или /aisettings key убрать.")
                return
            store.set_ai("api_key", "" if value == "убрать" else ai_cipher.encrypt(value.encode()).decode())
            try:
                await message.delete()
            except TelegramAPIError:
                pass
        elif field in ("model", "mode", "prompt", "facts", "url"):
            if not value or len(value) > (4000 if field in ("prompt", "facts") else 200):
                await message.reply("Укажите значение после названия настройки; инструкции — до 4000 символов.")
                return
            if field == "mode" and value not in ("auto", "draft", "off"):
                await message.reply("Режим: auto, draft или off.")
                return
            if field == "url":
                address = urlsplit(value)
                if address.scheme != "https" or not address.hostname or address.username or address.password or address.query or address.fragment:
                    await message.reply("Укажите HTTPS-адрес API без ключа, например https://openrouter.ai/api/v1.")
                    return
                store.set_ai("provider", "custom")
                store.set_ai("api_key", "")
            store.set_ai({"facts": "knowledge", "url": "base_url"}.get(field, field), value)
        elif field == "show":
            options = store.ai_options(ai_cipher)
            text = "Инструкция:\n" + options["prompt"][:1800] + "\n\nФакты:\n" + options["knowledge"][:1800]
            while utf16_length(text) > 4000:
                text = text[:-1]
            await message.reply(text)
            return
        else:
            await message.reply("Используйте /aisettings без аргументов для инструкции.")
            return
        await message.answer("✅ Настройка сохранена и уже применяется. Проверьте ИИ командой /aitest.")

    @router.message(Command("aitest"))
    async def ai_test(message: Message, bot: Bot):
        if not message.from_user or message.sender_chat or not await is_owner(message.from_user.id, bot):
            return
        await message.reply("Проверяю API ИИ…")
        try:
            answer = await generate_ai_answer("Поздоровайся одним предложением.", settings=store.ai_options(ai_cipher))
        except AIUnavailable as error:
            await message.reply(str(error))
            return
        await message.reply("✅ API работает. Ответ:\n" + answer)

    @router.message(Command("human"), F.chat.type == "private")
    async def human_command(message: Message, bot: Bot):
        if message.from_user and not store.banned(message.from_user.id):
            await handoff(message, bot)

    @router.message(Command("new"), F.chat.type == "private")
    async def new_dialogue(message: Message):
        if message.from_user and not store.banned(message.from_user.id):
            store.save_dialogue(message.from_user.id, False)
            await message.answer("Начали новый разговор. Напишите вопрос; если нужен сотрудник, используйте /human.")

    @router.message(Command("airesume"))
    async def ai_resume(message: Message):
        if not await staff_allowed(message):
            return
        target = store.target(message)
        if not target:
            await message.reply("Ответьте /airesume на вопрос пользователя.")
            return
        store.save_dialogue(target["user_id"], False)
        await message.reply("ИИ снова будет отвечать на следующие сообщения пользователя.")

    @router.callback_query(F.data == "human")
    async def human_callback(callback: CallbackQuery, bot: Bot):
        if not callback.message or callback.message.chat.type != "private" or callback.from_user.id != callback.message.chat.id:
            await callback.answer()
            return
        if store.banned(callback.from_user.id):
            await callback.answer("Доступ заблокирован.", show_alert=True)
            return
        await callback.answer()
        dialogue = store.dialogue(callback.from_user.id)
        question = next((item["content"] for item in reversed(dialogue["history"]) if item["role"] == "user"), "Нужен сотрудник поддержки.")
        source = Message(message_id=dialogue["source_id"] or callback.message.message_id, date=callback.message.date,
            chat=callback.message.chat, from_user=callback.from_user, text=question).as_(bot)
        await handoff(source, bot)

    @router.message(Command("ai"))
    async def ai_command(message: Message, bot: Bot, command: CommandObject):
        if not await staff_allowed(message):
            return
        if store.ai_options(ai_cipher)["mode"] == "off":
            await message.reply("ИИ выключен владельцем. Включение: /aisettings mode draft или auto.")
            return
        target = store.target(message)
        if not target or not target["incoming"]:
            await message.reply("Отправьте /ai ответом на пересланный текст вопроса игрока.")
            return
        if store.banned(target["user_id"]):
            await message.reply("Пользователь заблокирован.")
            return
        if not store.profile(message.from_user.id)["nickname"]:
            await message.reply("Сначала установите свою подпись: /nickname ВашНик.")
            return
        question = message.reply_to_message.text or message.reply_to_message.caption
        if not question or question.startswith("📩 Вопрос в поддержку"):
            await message.reply("Ответьте командой /ai на текст вопроса или подпись к медиа, а не на карточку пользователя.")
            return
        try:
            answer = await generate_ai_answer(question, command.args or "", settings=store.ai_options(ai_cipher))
        except AIUnavailable as error:
            await message.reply(str(error))
            return
        with store.db:
            cursor = store.db.execute("INSERT INTO ai_drafts (chat_id, target_id, staff_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (message.chat.id, message.reply_to_message.message_id, message.from_user.id, answer, time.time()))
            draft_id = cursor.lastrowid
        await message.reply("🤖 Черновик ИИ — проверьте перед отправкой:\n\n" + answer,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📨 Отправить игроку", callback_data=f"ai_send:{draft_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"ai_cancel:{draft_id}")]]))

    @router.callback_query(F.data.regexp(r"^ai_(send|cancel):\d+$"))
    async def ai_callback(callback: CallbackQuery, bot: Bot):
        if not callback.message:
            await callback.answer()
            return
        action, draft_id = callback.data.split(":")
        draft = store.db.execute("SELECT * FROM ai_drafts WHERE id = ? AND chat_id = ?", (int(draft_id), callback.message.chat.id)).fetchone()
        if not draft or draft["created_at"] <= time.time() - 600:
            if draft:
                with store.db:
                    store.db.execute("DELETE FROM ai_drafts WHERE id = ?", (draft["id"],))
            await callback.answer("Черновик истёк. Снова используйте /ai.", show_alert=True)
            return
        if callback.from_user.id != draft["staff_id"]:
            await callback.answer("Отправить черновик может только сотрудник, который его запросил.", show_alert=True)
            return
        if action == "ai_send" and callback.message.chat.id == store.group_id:
            member = await bot.get_chat_member(store.group_id, callback.from_user.id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) or (member.status == ChatMemberStatus.RESTRICTED and not member.is_member):
                await callback.answer("Вы не участник группы поддержки.", show_alert=True)
                return
        await callback.answer()
        if action == "ai_send":
            source = Message(message_id=draft["target_id"], date=callback.message.date, chat=callback.message.chat)
            outgoing_message = Message(message_id=callback.message.message_id, date=callback.message.date,
                chat=callback.message.chat, from_user=callback.from_user, text=draft["text"],
                reply_to_message=source, message_thread_id=callback.message.message_thread_id).as_(bot)
            if not await outgoing(outgoing_message, bot):
                return
        with store.db:
            store.db.execute("DELETE FROM ai_drafts WHERE id = ?", (draft["id"],))
        await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(Command("myid"))
    async def myid(message: Message):
        if message.from_user and not message.sender_chat:
            await message.reply(f"Ваш Telegram ID: {message.from_user.id}\nID чата: {message.chat.id}")

    @router.message(Command("setgroup"), F.chat.type.in_({"group", "supergroup"}))
    async def setgroup(message: Message, bot: Bot):
        if not message.from_user or message.sender_chat or message.from_user.id != owner_id:
            await message.reply("Привязать группу может только пользователь, который подключил этого бота.")
            return
        bot_member = await bot.get_chat_member(message.chat.id, bot.id)
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            await message.reply("Сначала добавьте бота администратором группы, чтобы он видел ответы всех участников.")
            return
        owner_member = await bot.get_chat_member(message.chat.id, owner_id)
        if owner_member.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            await message.reply("Для привязки вы должны быть администратором этой группы.")
            return
        chat = await bot.get_chat(message.chat.id)
        if chat.is_forum and not (bot_member.can_manage_topics and bot_member.can_delete_messages):
            await message.reply("Для отдельных обращений дайте боту права «Управление темами» и «Удаление сообщений», затем повторите /setgroup.")
            return
        old_group = store.group_id
        store.set_group(message.chat.id)
        store.set_topics(bool(chat.is_forum))
        try:
            if old_group and old_group != message.chat.id:
                await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=old_group))
            await bot.set_my_commands(group_commands(), scope=BotCommandScopeChat(chat_id=message.chat.id))
        except TelegramAPIError:
            logging.warning("Не удалось обновить меню команд группы")
        mode = ("Для каждого пользователя будет отдельная тема. /close закрывает обращение.\n" if chat.is_forum else
                "Для отдельных обращений включите «Темы» в настройках группы и повторите /setgroup.\n")
        await message.reply("✅ Группа поддержки подключена.\n" + mode + "Отвечайте на вопросы через «Ответить».\n"
                            "Подпись: /nickname ВашНик\nРоли выдаются владельцем бота или группы: /role Куратор ответом на сотрудника.")

    @router.message(Command("start", "help"))
    async def help_message(message: Message, bot: Bot, command: CommandObject):
        if message.chat.type in ("group", "supergroup") and command.args == "connect":
            await setgroup(message, bot)
            return
        if message.chat.type == "private":
            if not message.from_user:
                return
            if store.banned(message.from_user.id):
                await message.answer("⛔ Доступ к поддержке заблокирован.")
                return
            try:
                await message.answer(greeting(config.PREMIUM_EMOJI), parse_mode="HTML",
                                     link_preview_options=LinkPreviewOptions(is_disabled=True))
            except TelegramBadRequest:
                await message.answer(greeting(False), parse_mode="HTML",
                                     link_preview_options=LinkPreviewOptions(is_disabled=True))
            if message.from_user.id == owner_id:
                await message.answer("⚙️ Вы владелец этого бота. Вопросы приходят сюда, пока не подключена группа.\n"
                                     "Задайте /nickname ВашНик и отвечайте на вопросы через «Ответить».\n"
                                     "Настройки ИИ: /aisettings; проверка API: /aitest.\n"
                                     "Добавьте бота администратором в группу и отправьте /setgroup@" + (await bot.me()).username)
            else:
                await message.answer("🤖 При подключённом API сначала отвечает ИИ. Если нужен сотрудник, напишите /human.\n"
                                     "/new — начать новый разговор с ИИ.")
        elif await staff_allowed(message):
            await message.reply("Отвечайте через «Ответить» на сообщение пользователя или карточку от бота.\n"
                                "/nickname Ник — ваша подпись\n/role Владелец или Куратор — роль\n"
                                "/role убрать — скрыть роль\n/banuser — ответом на вопрос заблокировать автора\n"
                                "/unbanuser — ответом на вопрос или с Telegram ID разблокировать автора\n"
                                "/close — закрыть обращение в теме\n"
                                "/ai — черновик ответа от ИИ, ответом на текст вопроса\n"
                                "Ники доступны всем. Роли назначают владельцы бота или группы ответом на сотрудника. Блокировки — администраторы.")

    @router.message(Command("nickname"))
    async def nickname(message: Message, command: CommandObject):
        if not await staff_allowed(message):
            return
        name = (command.args or "").strip()
        if not name or len(name) > 40 or any(char in name for char in "\r\n[]") or any(ord(char) < 32 for char in name):
            await message.reply("Используйте /nickname ВашНик — до 40 символов, без переноса строк и квадратных скобок.")
            return
        store.set_profile(message.from_user.id, "nickname", name)
        await message.reply(f"✅ Ваш ник: {name}")

    @router.message(Command("role"))
    async def role(message: Message, command: CommandObject, bot: Bot):
        if not await staff_allowed(message):
            return
        if not await is_owner(message.from_user.id, bot):
            await message.reply("Назначать и снимать роли могут только владелец бота или владелец группы.")
            return
        args = (command.args or "").strip().split()
        value = args[0].casefold() if args else ""
        if value not in ("владелец", "куратор", "убрать") or len(args) > 2:
            await message.reply("Ответьте на сообщение сотрудника: /role Куратор или /role убрать.\nМожно указать ID: /role Куратор 123456789.")
            return
        target_user = message.reply_to_message.from_user if message.reply_to_message else message.from_user
        target_id = target_user.id if target_user and not target_user.is_bot else None
        if len(args) == 2:
            target_id = int(args[1]) if args[1].isdigit() and int(args[1]) > 0 else None
        if not target_id:
            await message.reply("Укажите Telegram ID сотрудника или ответьте на его сообщение от личного аккаунта.")
            return
        if value == "владелец" and not await is_owner(target_id, bot):
            await message.reply("Роль «Владелец» можно назначить только фактическому владельцу бота или группы.")
            return
        if value != "убрать" and target_id != owner_id:
            if not store.group_id:
                await message.reply("Сначала подключите группу поддержки.")
                return
            member = await bot.get_chat_member(store.group_id, target_id)
            if member.user.is_bot or member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) or (member.status == ChatMemberStatus.RESTRICTED and not member.is_member):
                await message.reply("Роль можно назначить только участнику группы поддержки.")
                return
        store.set_role(target_id, None if value == "убрать" else value.capitalize(), message.from_user.id)
        await message.reply(f"✅ Пользователь {target_id}: " + ("роль убрана." if value == "убрать" else f"роль {value.capitalize()}."))

    async def ensure_ticket(bot, user):
        ticket = store.ticket(store.group_id, user_id=user.id)
        now = time.time()
        if ticket and ((ticket["delete_at"] is not None and ticket["delete_at"] <= now) or
                       (ticket["delete_at"] is None and ticket["activity"] <= now - config.TOPIC_IDLE_HOURS * 3600)):
            await delete_ticket(bot, store, ticket)
            ticket = None
        if ticket and ticket["delete_at"] is not None:
            try:
                await bot.reopen_forum_topic(ticket["chat_id"], ticket["thread_id"])
            except TelegramBadRequest as error:
                if not topic_missing(error) and "topic_not_modified" not in str(error).lower():
                    raise
                if topic_missing(error):
                    store.forget_ticket(ticket)
                    ticket = None
            if ticket:
                with store.db:
                    store.db.execute("UPDATE tickets SET delete_at = NULL, activity = ? WHERE chat_id = ? AND user_id = ?",
                                     (now, ticket["chat_id"], user.id))
        if not ticket:
            label = "@" + user.username if user.username else user.full_name
            title = "Вопрос от пользователя · " + " ".join(label.split())
            while utf16_length(title) > 128:
                title = title[:-1]
            topic = await bot.create_forum_topic(store.group_id, title)
            store.save_ticket(store.group_id, user.id, topic.message_thread_id)
            ticket = store.ticket(store.group_id, user_id=user.id)
        return ticket

    async def close_ticket(bot, ticket):
        if ticket["delete_at"] is not None:
            return False
        try:
            await bot.close_forum_topic(ticket["chat_id"], ticket["thread_id"])
        except TelegramBadRequest as error:
            if "topic_not_modified" not in str(error).lower():
                raise
        with store.db:
            store.db.execute("UPDATE tickets SET delete_at = ? WHERE chat_id = ? AND thread_id = ?",
                (time.time() + config.CLOSED_TOPIC_DELETE_MINUTES * 60, ticket["chat_id"], ticket["thread_id"]))
        store.save_dialogue(ticket["user_id"], False)
        try:
            await bot.send_message(ticket["user_id"], "✅ Ваше обращение закрыто. Если нужна помощь, напишите сюда снова.")
        except TelegramAPIError:
            pass
        return True

    @router.message(Command("close"))
    async def close_command(message: Message, bot: Bot):
        if not await staff_allowed(message):
            return
        ticket = store.ticket(message.chat.id, thread_id=message.message_thread_id)
        if not ticket:
            await message.reply("Используйте /close внутри темы обращения.")
            return
        await message.reply(f"Закрываю обращение. Тема будет удалена через {config.CLOSED_TOPIC_DELETE_MINUTES} мин.")
        await close_ticket(bot, ticket)

    @router.callback_query(F.data.regexp(r"^close:\d+$"))
    async def close_callback(callback: CallbackQuery, bot: Bot):
        if not callback.message or callback.message.chat.id != store.group_id or callback.from_user.is_bot:
            await callback.answer("Обращение недоступно.", show_alert=True)
            return
        member = await bot.get_chat_member(store.group_id, callback.from_user.id)
        if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) or (member.status == ChatMemberStatus.RESTRICTED and not member.is_member):
            await callback.answer("Вы не участник группы поддержки.", show_alert=True)
            return
        ticket = store.ticket(store.group_id, thread_id=int(callback.data.split(":")[1]))
        if not ticket:
            await callback.answer("Тема уже удалена.")
            return
        await callback.answer("Закрываю обращение…")
        await close_ticket(bot, ticket)
        await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(F.chat.type == "supergroup", F.forum_topic_closed | F.forum_topic_reopened)
    async def topic_state(message: Message):
        ticket = store.ticket(message.chat.id, thread_id=message.message_thread_id)
        if not ticket:
            return
        if message.forum_topic_closed and ticket["delete_at"] is not None:
            return
        with store.db:
            store.db.execute("UPDATE tickets SET activity = ?, delete_at = ? WHERE chat_id = ? AND thread_id = ?",
                (time.time(), time.time() + config.CLOSED_TOPIC_DELETE_MINUTES * 60 if message.forum_topic_closed else None,
                 ticket["chat_id"], ticket["thread_id"]))

    @router.message(Command("banuser", "unbanuser"))
    async def banuser(message: Message, command: CommandObject, bot: Bot):
        if not await staff_allowed(message):
            return
        if not await admin_allowed(message, bot):
            await message.reply("Блокировать пользователей могут только владелец и администраторы группы.")
            return
        target = store.target(message)
        unban = command.command == "unbanuser"
        if target and target["incoming"] and message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot.id:
            user_id = target["user_id"]
        elif unban and (command.args or "").strip().isdigit() and int(command.args.strip()) > 0:
            user_id = int(command.args.strip())
        else:
            await message.reply("Отправьте команду ответом на карточку или пересланный вопрос от бота."
                                + (" Можно также использовать /unbanuser TelegramID." if unban else ""))
            return
        if user_id == owner_id:
            await message.reply("Владельца бота нельзя заблокировать.")
            return
        store.set_ban(user_id, not unban)
        await message.reply(f"{'✅ Разблокирован' if unban else '⛔ Заблокирован'} пользователь {user_id}.")
        try:
            await bot.send_message(user_id, "✅ Доступ к поддержке восстановлен." if unban
                                   else "⛔ Доступ к поддержке заблокирован.")
        except TelegramAPIError:
            pass

    def human_keyboard():
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 Позвать человека", callback_data="human")]])

    async def handoff(message, bot):
        user_id = message.from_user.id
        dialogue = store.dialogue(user_id)
        if dialogue["human"]:
            await deliver_to_staff(message, bot)
            await message.answer("Разговор уже передан сотрудникам. Напишите им сюда; /new начинает новый разговор с ИИ.")
            return
        if await deliver_to_staff(message, bot, dialogue["history"]):
            store.save_dialogue(user_id, True, message.message_id)
            await message.answer("👤 Передал разговор сотрудникам. Их ответы придут сюда.")

    @router.message(F.chat.type == "private")
    async def incoming(message: Message, bot: Bot):
        if not message.from_user or message.from_user.is_bot:
            return
        user = message.from_user
        if user.id == owner_id and store.target(message):
            await outgoing(message, bot)
            return
        if store.banned(user.id):
            await message.answer("⛔ Доступ к поддержке заблокирован.")
            return
        dialogue = store.dialogue(user.id)
        if message.message_id <= dialogue["source_id"]:
            return
        text = (message.text or "").strip()
        request_human = text.lower() in ("человек", "оператор", "сотрудник", "живой оператор") or re.search(
            r"(?:позов[иу]|позвать|соедин[иь]|перевед[иь]|хочу|нужен|нужна|связаться).{0,50}(?:человек|оператор|сотрудник|админ|куратор)", text, re.I)
        if request_human and user.id != owner_id:
            await handoff(message, bot)
            return
        options = store.ai_options(ai_cipher)
        if user.id != owner_id and options["mode"] == "auto" and options["api_key"] and not dialogue["human"]:
            if not message.text:
                await handoff(message, bot)
                return
            try:
                await bot.send_chat_action(message.chat.id, "typing")
                answer = await generate_ai_answer(text, settings=options, history=dialogue["history"])
                await message.answer("🤖 ИИ-помощник\n\n" + answer, reply_markup=human_keyboard(),
                    link_preview_options=LinkPreviewOptions(is_disabled=True))
            except AIUnavailable as error:
                logging.warning("ИИ бота %s: %s", bot.id, error)
                await handoff(message, bot)
                return
            history = dialogue["history"] + [{"role": "user", "content": text[:4000]},
                                             {"role": "assistant", "content": answer}]
            store.save_dialogue(user.id, False, message.message_id, history)
            store.record_received(user.id)
            return
        await deliver_to_staff(message, bot)

    async def deliver_to_staff(message: Message, bot: Bot, history=None):
        if not message.from_user or message.from_user.is_bot:
            return
        user = message.from_user
        if user.id == owner_id and store.target(message):
            await outgoing(message, bot)
            return
        if store.banned(user.id):
            await message.answer("⛔ Доступ к поддержке заблокирован.")
            return
        inbox = store.group_id or owner_id
        if store.db.execute("SELECT 1 FROM relays WHERE user_id = ? AND source_id = ? AND incoming = 1",
                            (user.id, message.message_id)).fetchone():
            return True
        for attempt in range(2):
            header = None
            ticket = None
            try:
                ticket = await ensure_ticket(bot, user) if store.group_id and store.topics_enabled else None
                thread_id = ticket["thread_id"] if ticket else None
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Закрыть обращение", callback_data=f"close:{thread_id}")]]) if ticket else None
                username = f" · @{escape(user.username)}" if user.username else ""
                header = await bot.send_message(inbox,
                    f'📩 <b>Вопрос в поддержку</b>\n<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
                    f'{username}\nID: <code>{user.id}</code>\nОтветьте на это сообщение или на вопрос ниже.',
                    parse_mode="HTML", message_thread_id=thread_id, reply_markup=keyboard)
                try:
                    forwarded = await bot.forward_message(inbox, message.chat.id, message.message_id, message_thread_id=thread_id)
                except TelegramBadRequest:
                    forwarded = await bot.copy_message(inbox, message.chat.id, message.message_id, message_thread_id=thread_id,
                        reply_parameters=ReplyParameters(message_id=header.message_id))
                store.link(inbox, header.message_id, user.id, message.message_id)
                store.link(inbox, forwarded.message_id, user.id, message.message_id)
                if store.dialogue(user.id)["source_id"] != message.message_id:
                    store.record_received(user.id)
                store.touch_ticket(inbox, user.id)
                if history:
                    context = "\n\n".join(("Игрок: " if item["role"] == "user" else "ИИ: ") + item["content"] for item in history)
                    try:
                        await bot.send_message(inbox, "🤖 Переписка перед передачей сотруднику:\n\n" + context[-1800:],
                            message_thread_id=thread_id, reply_parameters=ReplyParameters(message_id=header.message_id))
                    except TelegramAPIError:
                        logging.warning("Не удалось передать историю ИИ бота %s", bot.id)
                return True
            except TelegramAPIError as error:
                if header:
                    try:
                        await bot.delete_message(inbox, header.message_id)
                    except TelegramAPIError:
                        pass
                if ticket and isinstance(error, TelegramBadRequest) and topic_missing(error):
                    store.forget_ticket(ticket)
                    if attempt == 0:
                        continue
                logging.warning("Не удалось передать вопрос в поддержку")
                await message.answer("Не удалось передать сообщение. Попробуйте отправить его позже или обычным текстом.")
                return

    @router.message(F.chat.type.in_({"group", "supergroup"}))
    async def outgoing(message: Message, bot: Bot):
        if message.migrate_to_chat_id and message.chat.id == store.group_id:
            store.set_group(message.migrate_to_chat_id)
            return
        if not message.reply_to_message or not await staff_allowed(message):
            return
        if message.text and message.text.startswith("/"):
            return
        target = store.target(message)
        if not target:
            return
        if store.banned(target["user_id"]):
            await message.reply("Пользователь заблокирован. Сначала используйте /unbanuser.")
            return
        profile = store.profile(message.from_user.id)
        if not profile["nickname"]:
            await message.reply("Сначала установите свой ник: /nickname ВашНик. Затем повторно отправьте ответ.")
            return
        role_name = profile["role"]
        if role_name == "Владелец" and not await is_owner(message.from_user.id, bot):
            role_name = None
        signature = profile["nickname"] + (f" [{role_name}]" if role_name else "")
        reply = ReplyParameters(message_id=target["source_id"], allow_sending_without_reply=True)
        try:
            if message.text and utf16_length(signature + "\n\n" + message.text) <= 4096:
                text, entities = signed_text(signature, message.text, message.entities)
                await bot.send_message(target["user_id"], text, entities=entities, reply_parameters=reply,
                                       link_preview_options=message.link_preview_options)
            elif any((message.photo, message.video, message.animation, message.document, message.audio, message.voice)) and utf16_length(signature + "\n\n" + (message.caption or "")) <= 1024:
                caption, entities = signed_text(signature, message.caption or "", message.caption_entities)
                await bot.copy_message(target["user_id"], message.chat.id, message.message_id,
                                       caption=caption, caption_entities=entities, reply_parameters=reply)
            else:
                label = await bot.send_message(target["user_id"], signature,
                    entities=[MessageEntity(type="bold", offset=0, length=utf16_length(signature))], reply_parameters=reply)
                try:
                    await bot.copy_message(target["user_id"], message.chat.id, message.message_id,
                        reply_parameters=ReplyParameters(message_id=label.message_id))
                except TelegramAPIError:
                    try:
                        await bot.delete_message(target["user_id"], label.message_id)
                    except TelegramAPIError:
                        pass
                    raise
            store.link(message.chat.id, message.message_id, target["user_id"], target["source_id"], incoming=False)
            store.touch_ticket(message.chat.id, target["user_id"])
            store.save_dialogue(target["user_id"], True, store.dialogue(target["user_id"])["source_id"])
        except TelegramForbiddenError:
            await message.reply("Ответ не доставлен: пользователь заблокировал бота или удалил аккаунт.")
            return
        except TelegramAPIError:
            await message.reply("Ответ не доставлен. Попробуйте ещё раз или отправьте обычный текст.")
            return
        await message.reply("✅ Ответ доставлен.")
        return True

    @router.errors()
    async def error_handler(event: ErrorEvent):
        logging.error("Ошибка обработки обновления: %s", type(event.exception).__name__)
        if event.update.message:
            try:
                await event.update.message.reply("Не удалось выполнить действие. Попробуйте ещё раз.")
            except TelegramAPIError:
                pass
        return True

    return router


if __name__ == "__main__":
    from manager import run
    run()
