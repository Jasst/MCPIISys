"""Проверки очистки рассуждений модели и разбора гипотез.

Тяжёлые библиотеки (эмбеддинги, поиск, mcp) подменяются заглушками: проверяется
логика очистки и повтора, а не сама модель.
"""
import asyncio
import importlib.abc
import importlib.machinery
import sys
from unittest.mock import MagicMock

HEAVY = ("sentence_transformers", "faiss", "torch", "transformers", "sklearn", "trafilatura",
         "ddgs", "duckduckgo_search", "bs4", "PIL", "pytesseract", "playwright", "markdownify",
         "readability", "lxml", "networkx", "pypdf", "docx", "openpyxl", "pandas", "yt_dlp")


class _Stub(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in HEAVY:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)

    def create_module(self, spec):
        m = MagicMock(); m.__path__ = []; m.__spec__ = spec; m.__name__ = spec.name
        return m

    def exec_module(self, m):
        pass


sys.meta_path.insert(0, _Stub())

from GCN.llm_text import strip_thinking, parse_hypotheses, looks_like_reasoning  # noqa: E402
import GCN.llm_client as llm_client  # noqa: E402
import routes.ai_assistant as ai  # noqa: E402

# реальный текст из лога: модель вывела рассуждения прямо в ответе
REASONING_LOG = (
    "Thinking Process:\n\n1.  **Analyze the Request:**\n"
    "    *   Task: Formulate 3 clear, testable hypotheses regarding the query.\n"
    "    *   Tag: `/no_think`\n"
    "2.  **Analyze the Context/Memory Data:**\n"
    "    *   Draft 1: Использование уточнения «курс доллара США к рублю» повысит релевантность.\n"
)


def test_strip_closed_block():
    assert strip_thinking("<think>размышляю</think>\nГипотеза одна") == "Гипотеза одна"


def test_strip_unclosed_block_drops_tail():
    assert strip_thinking("ответ\n<think>и дальше думаю без конца") == "ответ"


def test_strip_closing_only_keeps_after_tag():
    assert strip_thinking("всё это рассуждение</think>- Итог") == "- Итог"


def test_plain_text_unchanged():
    assert strip_thinking("- обычный список") == "- обычный список"


def test_reasoning_is_detected_and_rejected():
    assert looks_like_reasoning(REASONING_LOG)
    assert parse_hypotheses(REASONING_LOG) == []


def test_parse_takes_only_clean_list_items():
    text = ("- Добавление пары USD/RUB повышает долю официальных котировок в выдаче\n"
            "- Какой курс доллара сегодня?\n"          # вопрос — отбрасывается
            "- коротко\n"                                # слишком короткий
            "- Добавление пары USD/RUB повышает долю официальных котировок в выдаче\n"  # дубль
            "- Слово «сегодня» снижает долю устаревших котировок в выдаче\n"
            "- Императивный запрос снижает точность выдачи финансовых данных\n"
            "- Четвёртая гипотеза не должна попасть в результат, лимит три")
    hs = parse_hypotheses(text)
    assert len(hs) == 3
    assert all(not h.endswith("?") and "коротко" != h for h in hs)
    assert len(set(hs)) == 3


def test_call_llm_strips_reasoning_for_service_calls(monkeypatch=None):
    async def fake_raw(messages, **kw):
        return {"content": "<think>думаю</think>{\"verdict\": \"ok\"}"}
    orig = llm_client.call_llm_raw
    llm_client.call_llm_raw = fake_raw
    try:
        out = asyncio.run(llm_client.call_llm([{"role": "user", "content": "x"}]))
    finally:
        llm_client.call_llm_raw = orig
    assert out == '{"verdict": "ok"}', out


def test_generate_hypotheses_retries_then_accepts():
    calls = []
    answers = [REASONING_LOG,
               "- Добавление пары USD/RUB повышает долю официальных котировок\n"
               "- Слово «сегодня» снижает долю устаревших котировок в выдаче\n"
               "- Императивная формулировка снижает точность выдачи финансовых данных"]

    async def fake_call(messages, temp=0.7, max_tokens=0, **kw):
        calls.append((temp, max_tokens))
        return answers[len(calls) - 1]

    orig = ai.call_llm
    ai.call_llm = fake_call
    try:
        hs = asyncio.run(ai.CognitiveController._generate_hypotheses(
            None, "курс доллара на сегодня", ""))
    finally:
        ai.call_llm = orig
    assert len(calls) == 2 and calls[1][0] < calls[0][0]   # повтор с меньшей температурой
    assert len(hs) == 3


def test_generate_hypotheses_gives_empty_list_not_reasoning():
    async def fake_call(messages, temp=0.7, max_tokens=0, **kw):
        return REASONING_LOG
    orig = ai.call_llm
    ai.call_llm = fake_call
    try:
        hs = asyncio.run(ai.CognitiveController._generate_hypotheses(
            None, "курс доллара на сегодня", ""))
    finally:
        ai.call_llm = orig
    assert hs == []   # рассуждения не превращаются в гипотезы и поисковые запросы


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok:", name)
    print("ALL OK")
