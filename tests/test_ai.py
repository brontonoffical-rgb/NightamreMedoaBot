import asyncio
import unittest
from unittest.mock import patch

from aiohttp import web

import config
from bot import AIUnavailable, generate_ai_answer, utf16_length


class AITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.status = 200
        self.body = {"choices": [{"message": {"content": "Уточните версию игры."}}]}
        self.requests = []
        application = web.Application()
        application.router.add_post("/chat/completions", self.handle)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base_url = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        self.key = patch.object(config, "AI_API_KEY", "test-key")
        self.url = patch.object(config, "AI_BASE_URL", self.base_url)
        self.model = patch.object(config, "AI_MODEL", "test-model")
        self.key.start()
        self.url.start()
        self.model.start()

    async def asyncTearDown(self):
        self.url.stop()
        self.key.stop()
        self.model.stop()
        await self.runner.cleanup()

    async def handle(self, request):
        self.requests.append((request.headers, await request.json()))
        return web.json_response(self.body, status=self.status)

    async def test_api_receives_key_and_support_context(self):
        answer = await generate_ai_answer("Не работает игра", "Кратко")
        self.assertEqual(answer, "Уточните версию игры.")
        headers, body = self.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(body["max_tokens"], 600)
        self.assertEqual(body["model"], config.AI_MODEL)
        self.assertIn(config.AI_KNOWLEDGE, body["messages"][0]["content"])
        self.assertIn("Кратко", body["messages"][1]["content"])

    async def test_personal_key_uses_openai_compatible_endpoint(self):
        with patch.object(config, "AI_API_KEY", "test-key"):
            with patch.object(config, "AI_BASE_URL", self.base_url):
                await generate_ai_answer("Вопрос")
        headers, body = self.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(body["model"], config.AI_MODEL)

    async def test_error_statuses_have_clear_messages(self):
        for status, phrase in [(401, "API-ключ"), (403, "доступе"), (429, "лимит"), (402, "баланс"), (404, "не найдены"), (500, "HTTP 500")]:
            self.status = status
            with self.assertRaisesRegex(AIUnavailable, phrase):
                await generate_ai_answer("Вопрос")

    async def test_missing_key_does_not_use_anonymous_endpoint(self):
        with patch.object(config, "AI_API_KEY", ""):
            with self.assertRaisesRegex(AIUnavailable, "личный API-ключ"):
                await generate_ai_answer("Вопрос")
        self.assertEqual(self.requests, [])

    async def test_per_bot_settings_and_conversation_history_are_used(self):
        settings = {"api_key": "per-bot-key", "base_url": self.base_url, "model": "chosen-model",
                    "prompt": "Ответь вежливо.", "knowledge": "Цена неизвестна."}
        history = [{"role": "user", "content": "Прошлый вопрос"}, {"role": "assistant", "content": "Прошлый ответ"}]
        await generate_ai_answer("Уточнение", settings=settings, history=history)
        headers, body = self.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer per-bot-key")
        self.assertEqual(body["model"], "chosen-model")
        self.assertEqual(body["messages"][1:3], history)
        self.assertIn("Цена неизвестна.", body["messages"][0]["content"])

    async def test_transient_server_error_is_retried(self):
        self.status = 503
        with patch("bot.asyncio.sleep", side_effect=self.set_success):
            answer = await generate_ai_answer("Вопрос")
        self.assertEqual(answer, "Уточните версию игры.")
        self.assertEqual(len(self.requests), 2)

    async def set_success(self, duration):
        self.status = 200

    async def test_malformed_and_empty_answers_are_rejected(self):
        for body in [{}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}]:
            self.body = body
            with self.assertRaises(AIUnavailable):
                await generate_ai_answer("Вопрос")

    async def test_output_is_bounded_in_telegram_utf16_units(self):
        self.body = {"choices": [{"message": {"content": "🌙" * 4000}}]}
        answer = await generate_ai_answer("Вопрос")
        self.assertLessEqual(utf16_length(answer), 3500)

    async def test_network_timeout_keeps_normal_support_available(self):
        class TimeoutSession:
            def post(self, *args, **kwargs):
                raise asyncio.TimeoutError
        with self.assertRaisesRegex(AIUnavailable, "поддержка продолжает работать"):
            await generate_ai_answer("Вопрос", session=TimeoutSession())
