import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import suppress
from html import escape
from pathlib import Path

if __name__ == "__main__" and os.name == "nt" and not os.getenv("NIGHTMARE_LAUNCHER"):
    import subprocess
    raise SystemExit(subprocess.call(["cmd.exe", "/c", str(Path(__file__).with_name("start.bat"))]))

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message
from cryptography.fernet import Fernet, InvalidToken

import config
from bot import Store, create_router, group_commands, topic_cleanup_loop


def extract_token(text):
    match = re.search(r"(?<![\w:])\d{5,}:[A-Za-z0-9_-]{20,}(?![\w:-])", text or "")
    return match.group() if match else None


def home_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add")],
        [InlineKeyboardButton(text="🤖 Мои боты", callback_data="list:0")],
        [InlineKeyboardButton(text="📖 Как это работает", callback_data="help")]])


def acquire_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "service.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        raise ValueError("Главный бот уже запущен из этой папки. Сначала остановите работающий экземпляр.") from None


class BotManager:
    def __init__(self, main_bot, directory, bot_factory=Bot):
        self.bot = main_bot
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        key_path = self.directory / "tokens.key"
        db_path = self.directory / "manager.sqlite3"
        if not key_path.exists():
            if db_path.exists():
                raise ValueError("Не найден data/tokens.key. Восстановите ключ из резервной копии.")
            with key_path.open("xb") as key_file:
                key_file.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS bots (
                bot_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, username TEXT NOT NULL,
                token BLOB, active INTEGER NOT NULL DEFAULT 1
            );
        """)
        self.factory = bot_factory
        self.workers = {}
        self.waiting = set()
        self.lock = asyncio.Lock()

    def owned(self, bot_id, owner_id):
        return self.db.execute("SELECT * FROM bots WHERE bot_id = ? AND owner_id = ?",
                               (bot_id, owner_id)).fetchone()

    def support_store(self, bot_id):
        return Store(self.directory / f"{bot_id}.sqlite3")

    async def configure(self, bot, store, owner_id=None):
        await bot.set_my_commands([BotCommand(command=name, description=description) for name, description in (
            ("start", "Написать в поддержку"), ("human", "Позвать сотрудника"),
            ("new", "Начать новый разговор"), ("help", "Помощь"), ("myid", "Узнать свой Telegram ID"))],
            scope=BotCommandScopeAllPrivateChats())
        if owner_id:
            with suppress(TelegramAPIError):
                await bot.set_my_commands([*group_commands(), BotCommand(command="start", description="Начать работу")],
                    scope=BotCommandScopeChat(chat_id=owner_id))
        if store.group_id:
            try:
                chat = await bot.get_chat(store.group_id)
                member = await bot.get_chat_member(store.group_id, bot.id)
                store.set_topics(bool(chat.is_forum))
                if chat.is_forum and not (member.status == "administrator" and member.can_manage_topics and member.can_delete_messages):
                    logging.warning("Боту %s нужны права управления темами и удаления сообщений", bot.id)
                await bot.set_my_commands(group_commands(), scope=BotCommandScopeChat(chat_id=store.group_id))
            except TelegramAPIError:
                logging.warning("Не удалось обновить меню группы бота %s", bot.id)
        await bot.delete_webhook(drop_pending_updates=False)

    async def poll(self, bot, store, owner_id):
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(store, owner_id, self.cipher))
        cleanup = asyncio.create_task(topic_cleanup_loop(bot, store))
        try:
            await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"], handle_as_tasks=False,
                handle_signals=False, close_bot_session=False)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.error("Бот %s остановлен: %s", bot.id, type(error).__name__)
            with suppress(TelegramAPIError):
                await self.bot.send_message(owner_id, "⚠️ Ваш бот остановлен. Откройте «Мои боты» и подключите его заново.")
        finally:
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)

    async def stop(self, bot_id):
        worker = self.workers.pop(bot_id, None)
        if worker:
            bot, task, store = worker
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await bot.session.close()
            store.db.close()

    async def connect(self, token, owner_id):
        async with self.lock:
            bot = self.factory(token)
            store = None
            retained = False
            try:
                me = await bot.get_me()
                if me.id == self.bot.id:
                    raise ValueError("Это токен главного бота. Пришлите токен другого бота из @BotFather.")
                existing = self.db.execute("SELECT * FROM bots WHERE bot_id = ?", (me.id,)).fetchone()
                if existing and existing["owner_id"] != owner_id:
                    raise ValueError("Этот бот уже подключён другим пользователем.")
                store = self.support_store(me.id)
                await self.stop(me.id)
                await self.configure(bot, store, owner_id)
                with self.db:
                    self.db.execute("""INSERT INTO bots VALUES (?, ?, ?, ?, 1)
                        ON CONFLICT(bot_id) DO UPDATE SET username = excluded.username,
                        token = excluded.token, active = 1""",
                        (me.id, owner_id, me.username, self.cipher.encrypt(token.encode())))
                task = asyncio.create_task(self.poll(bot, store, owner_id), name=f"support-{me.id}")
                self.workers[me.id] = (bot, task, store)
                retained = True
                return me
            finally:
                if not retained:
                    await bot.session.close()
                    if store:
                        store.db.close()

    async def disconnect(self, bot_id, owner_id):
        async with self.lock:
            if not self.owned(bot_id, owner_id):
                return False
            await self.stop(bot_id)
            with self.db:
                self.db.execute("UPDATE bots SET active = 0, token = NULL WHERE bot_id = ?", (bot_id,))
            return True

    async def restore(self):
        rows = self.db.execute("SELECT * FROM bots WHERE active = 1").fetchall()
        for row in rows:
            try:
                token = self.cipher.decrypt(row["token"]).decode()
                await self.connect(token, row["owner_id"])
            except (TelegramAPIError, ValueError, InvalidToken):
                logging.warning("Не удалось запустить подключённого бота %s", row["bot_id"])
                with suppress(TelegramAPIError):
                    await self.bot.send_message(row["owner_id"],
                        f"⚠️ Не удалось запустить @{row['username']}. Пришлите его актуальный токен через /addbot.")

    async def close(self):
        for bot_id in list(self.workers):
            await self.stop(bot_id)
        self.db.close()

    def bot_list(self, owner_id, page=0):
        total = self.db.execute("SELECT COUNT(*) FROM bots WHERE owner_id = ?", (owner_id,)).fetchone()[0]
        page = min(max(page, 0), max((total - 1) // 8, 0))
        rows = self.db.execute("SELECT * FROM bots WHERE owner_id = ? ORDER BY bot_id LIMIT 8 OFFSET ?",
                               (owner_id, page * 8)).fetchall()
        keyboard = []
        for row in rows:
            worker = self.workers.get(row["bot_id"])
            status = "🟢" if worker and not worker[1].done() else "🟠" if row["active"] else "⚪"
            keyboard.append([InlineKeyboardButton(text=f"{status} @{row['username']}", callback_data=f"bot:{row['bot_id']}")])
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text="← Назад", callback_data=f"list:{page - 1}"))
        if (page + 1) * 8 < total:
            navigation.append(InlineKeyboardButton(text="Далее →", callback_data=f"list:{page + 1}"))
        if navigation:
            keyboard.append(navigation)
        keyboard.append([InlineKeyboardButton(text="➕ Добавить бота", callback_data="add")])
        return ("🤖 <b>Мои боты</b>\nВыберите бота для настройки." if rows else
                "🤖 <b>Пока нет подключённых ботов.</b>\nНажмите «Добавить бота» и пришлите токен из @BotFather."), InlineKeyboardMarkup(inline_keyboard=keyboard)

    def card(self, bot_id, owner_id):
        row = self.owned(bot_id, owner_id)
        if not row:
            return None
        store = self.support_store(bot_id)
        try:
            users = store.db.execute("SELECT COUNT(*) FROM visitors").fetchone()[0]
            questions = store.db.execute("SELECT value FROM settings WHERE key = 'questions_count'").fetchone()[0]
            bans = store.db.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
            topics = store.db.execute("SELECT COUNT(*) FROM tickets WHERE delete_at IS NULL").fetchone()[0]
            group_id = store.group_id
        finally:
            store.db.close()
        worker = self.workers.get(bot_id)
        online = worker is not None and not worker[1].done()
        status = "🟢 Работает" if online else "⚪ Отключён" if not row["active"] else "🟠 Требуется переподключение"
        inbox = f"Группа: <code>{group_id}</code>" if group_id else "Вопросы приходят вам в личку через этого бота."
        text = (f"🤖 <b>@{escape(row['username'])}</b>\n{status}\n\n{inbox}\n\n"
                f"👥 Пользователей: {users}\n📩 Вопросов: {questions}\n🗂 Открытых тем: {topics}\n⛔ Заблокировано: {bans}\n\n"
                f"Для группы: включите «Темы», дайте боту права управления темами и удаления сообщений, затем отправьте\n<code>/setgroup@{row['username']}</code>\n"
                "Подпись: <code>/nickname ВашНик</code>\nРоли выдаёт владелец бота или группы ответом на сотрудника: <code>/role Куратор</code>.\n"
                "ИИ настраивается в личке подключённого бота: <code>/aisettings</code>; проверка: <code>/aitest</code>.")
        buttons = [[InlineKeyboardButton(text="💬 Открыть бота", url=f"https://t.me/{row['username']}")]]
        if row["active"]:
            buttons.extend([
                [InlineKeyboardButton(text="👥 Добавить в группу", url=f"https://t.me/{row['username']}?startgroup=connect&admin=manage_topics+delete_messages")],
                [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"bot:{bot_id}"),
                 InlineKeyboardButton(text="⏹ Отключить", callback_data=f"disconnect:{bot_id}")]])
        else:
            buttons.append([InlineKeyboardButton(text="🔌 Подключить снова", callback_data="add")])
        buttons.append([InlineKeyboardButton(text="← Мои боты", callback_data="list:0")])
        return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def create_manager_router(manager):
    router = Router()
    router.message.filter(F.chat.type == "private")

    async def show(message, text, keyboard):
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    async def add_prompt(message):
        manager.waiting.add(message.chat.id)
        await show(message, "➕ <b>Подключение бота</b>\n\n"
            "1. Откройте @BotFather и создайте бота через /newbot.\n"
            "2. Скопируйте его API Token и отправьте сюда одним сообщением.\n\n"
            "Можно переслать сообщение BotFather с токеном.\n"
            "Для отмены: /cancel", InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🤖 Открыть BotFather", url="https://t.me/BotFather")],
                [InlineKeyboardButton(text="Отмена", callback_data="cancel")]]))

    @router.message(Command("start", "help"))
    async def welcome(message: Message):
        manager.waiting.discard(message.chat.id)
        await show(message, "🌙 <b>Nightmare · конструктор поддержки</b>\n\n"
            "Подключите своего Telegram-бота и принимайте вопросы от пользователей.\n"
            "Отвечайте сами или добавьте группу, чтобы помогать всей командой.\n\n"
            "➕ <b>Добавить бота</b> — подключить токен из BotFather.\n"
            "🤖 <b>Мои боты</b> — группа, статистика и управление.\n\n"
            "Ответы подписываются вашим ником и ролью: <b>Nightmare [Владелец]</b>.", home_keyboard())

    @router.message(Command("addbot", "connect"))
    async def addbot(message: Message):
        await add_prompt(message)

    @router.message(Command("bots", "mybots"))
    async def bots(message: Message):
        manager.waiting.discard(message.chat.id)
        await show(message, *manager.bot_list(message.chat.id))

    @router.message(Command("cancel"))
    async def cancel(message: Message):
        manager.waiting.discard(message.chat.id)
        await show(message, "Подключение отменено.", home_keyboard())

    @router.callback_query(F.data.in_({"add", "cancel", "help"}))
    async def home_action(callback: CallbackQuery):
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer()
            return
        await callback.answer()
        if callback.data == "add":
            await add_prompt(callback.message)
        elif callback.data == "cancel":
            await cancel(callback.message)
        else:
            await welcome(callback.message)

    @router.callback_query(F.data.regexp(r"^(list|bot|disconnect):\d+$"))
    async def manage_bot(callback: CallbackQuery):
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer()
            return
        action, number = callback.data.split(":")
        number = int(number)
        owner_id = callback.from_user.id
        if action != "list" and not manager.owned(number, owner_id):
            await callback.answer("Этот бот не принадлежит вам.", show_alert=True)
            return
        await callback.answer()
        manager.waiting.discard(owner_id)
        if action == "list":
            result = manager.bot_list(owner_id, number)
        else:
            if action == "disconnect":
                await manager.disconnect(number, owner_id)
            result = manager.card(number, owner_id)
        await show(callback.message, *result)

    @router.message()
    async def token_message(message: Message):
        if not message.from_user or message.from_user.is_bot:
            return
        token = extract_token(message.text or message.caption)
        if not token:
            if message.chat.id in manager.waiting:
                await message.answer("Это не похоже на токен. Скопируйте API Token из @BotFather или отправьте /cancel.")
            else:
                await show(message, "Выберите действие ниже.", home_keyboard())
            return
        with suppress(TelegramAPIError):
            await message.delete()
        try:
            me = await manager.connect(token, message.from_user.id)
        except ValueError as error:
            await message.answer(str(error))
            return
        except TelegramAPIError:
            await message.answer("Не удалось подключить бота. Проверьте токен в @BotFather и попробуйте ещё раз.")
            return
        manager.waiting.discard(message.chat.id)
        await message.answer(f"✅ @{me.username} подключён!\nОткройте его и нажмите «Запустить», чтобы получать вопросы.\n"
                             "Для команды добавьте его в группу администратором.")
        await show(message, *manager.card(me.id, message.from_user.id))

    @router.errors()
    async def manager_error(event: ErrorEvent):
        logging.error("Ошибка главного бота: %s", type(event.exception).__name__)
        message = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
        if message:
            with suppress(TelegramAPIError):
                await message.answer("Не удалось выполнить действие. Попробуйте ещё раз.")
        return True

    return router


class TokenFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if config.AI_API_KEY:
            message = message.replace(config.AI_API_KEY, "[API_KEY]")
        record.msg = re.sub(r"\d{5,}:[A-Za-z0-9_-]{20,}", "[TOKEN]", message)
        record.args = ()
        return True


async def configure_main(bot):
    for attempt in range(5):
        try:
            me = await bot.get_me()
            await bot.set_my_commands([BotCommand(command=name, description=description) for name, description in (
                ("start", "Главное меню"), ("addbot", "Подключить своего бота"), ("bots", "Мои боты"),
                ("help", "Как это работает"), ("cancel", "Отменить подключение"))])
            await bot.delete_webhook(drop_pending_updates=False)
            return me
        except TelegramNetworkError:
            if attempt == 4:
                raise
            logging.warning("Нет связи с Telegram. Повтор подключения при запуске.")
            await asyncio.sleep(min(2 ** (attempt + 1), 15))


async def main():
    if extract_token(config.BOT_TOKEN) != config.BOT_TOKEN:
        print("Откройте config.py и вставьте токен ГЛАВНОГО бота из @BotFather в BOT_TOKEN.")
        return
    bot = Bot(config.BOT_TOKEN)
    manager = None
    instance_lock = None
    runtime_path = config.DATA_DIR / "runtime.json"
    try:
        instance_lock = acquire_lock(config.DATA_DIR)
        me = await configure_main(bot)
        manager = BotManager(bot, config.DATA_DIR)
        await manager.restore()
        runtime_path.write_text(json.dumps({"pid": os.getpid(), "username": me.username}), encoding="utf-8")
        dispatcher = Dispatcher()
        dispatcher.include_router(create_manager_router(manager))
        print(f"Главный бот @{me.username} запущен. Откройте его в Telegram. Остановка: Ctrl+C.", flush=True)
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"],
            handle_as_tasks=False, close_bot_session=False)
    except TelegramAPIError as error:
        print(f"Ошибка Telegram: {type(error).__name__}. Проверьте токен и интернет.")
    except (ValueError, InvalidToken) as error:
        print(str(error))
    finally:
        if manager:
            await manager.close()
        await bot.session.close()
        if instance_lock:
            with suppress(OSError, ValueError):
                if json.loads(runtime_path.read_text(encoding="utf-8"))["pid"] == os.getpid():
                    runtime_path.unlink()
            instance_lock.close()


def run():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(TokenFilter())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
