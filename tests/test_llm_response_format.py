"""Проверка response_format в llm_client на настоящем HTTP-сервере (aiohttp).
Сценарии: бэкенд понимает схему; бэкенд отклоняет схему (400) -> повтор без неё.
Запуск: python -m unittest tests.test_llm_response_format -v
"""
import asyncio
import json
import os
import sys
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import GCN.llm_client as llm  # noqa: E402


def make_app(reject_schema: bool, seen: list):
    async def handler(request):
        body = await request.json()
        seen.append(body)
        if reject_schema and "response_format" in body:
            return web.Response(status=400, text="response_format is not supported")
        return web.json_response({"choices": [{"message": {"role": "assistant",
                                                           "content": '{"relation":"false_positive","keep":"both","reason":"x"}'}}]})
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    return app


async def _run(reject: bool):
    seen = []
    server = TestServer(make_app(reject, seen))
    await server.start_server()
    old_url = llm._chat_url
    llm._chat_url = lambda: str(server.make_url("/v1/chat/completions"))
    try:
        out = await llm.call_llm_raw([{"role": "user", "content": "hi"}], temp=0.0, max_tokens=50,
                                     retries=2, response_format={"type": "json_schema"})
    finally:
        llm._chat_url = old_url
        await llm.close_session()
        await server.close()
    return out, seen


class TestResponseFormat(unittest.TestCase):
    def test_schema_sent_when_supported(self):
        out, seen = asyncio.run(_run(reject=False))
        self.assertIn("response_format", seen[0])
        self.assertIn("relation", out.get("content", ""))

    def test_falls_back_without_schema_when_rejected(self):
        out, seen = asyncio.run(_run(reject=True))
        self.assertIn("response_format", seen[0])        # первая попытка со схемой
        self.assertNotIn("response_format", seen[-1])    # повтор без схемы
        self.assertIn("relation", out.get("content", ""))


if __name__ == "__main__":
    unittest.main()
