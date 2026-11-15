"""Тесты доски исследования и многоходового агентного цикла.

Сеть и LLM подменяются заглушками, поэтому тесты детерминированы и работают офлайн.
Запуск: python tests/test_research_loop.py  (или pytest tests/test_research_loop.py)
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AUTH_DISABLED", "1")

from GCN.research_board import (  # noqa: E402
    EvidenceBoard, parse_analysis, split_context_by_url, normalize_url,
    BOARD_RENDER_MAX_CHARS,
)
from GCN import internal_tools  # noqa: E402
from GCN.tool_router import ToolRegistry, ToolRouter  # noqa: E402


# ---------- доска ----------

def test_board_dedupes_urls_and_assigns_stable_ids():
    b = EvidenceBoard()
    b.reset("вопрос")
    a = b.add_source("https://example.com/a/", "A", "снипет")
    again = b.add_source("https://example.com/a", "", "более длинный снипет текста")
    c = b.add_source("https://example.com/c", "C")
    assert a.sid == "S1" and again.sid == "S1" and c.sid == "S2"
    assert b.sources["S1"].snippet == "более длинный снипет текста"
    assert b.resolve("S2").url == "https://example.com/c"
    assert b.resolve("https://example.com/c/") is c


def test_split_context_by_url_maps_text_to_sources():
    ctx = ("Источник: Первая\nURL: https://a.ru/x\nТекст первой страницы.\n\n---\n\n"
           "Источник: Вторая\nURL: https://b.ru/y\nТекст второй страницы.")
    parts = split_context_by_url(ctx)
    assert parts[normalize_url("https://a.ru/x")] == "Текст первой страницы."
    assert parts[normalize_url("https://b.ru/y")] == "Текст второй страницы."


def test_is_allowed_url_blocks_arbitrary_urls():
    b = EvidenceBoard()
    b.reset("посмотри https://user.example/page")
    b.add_source("https://found.example/x")
    assert b.is_allowed_url("https://found.example/x")
    assert b.is_allowed_url("https://user.example/page")          # из сообщения пользователя
    assert not b.is_allowed_url("http://169.254.169.254/latest")  # не из поиска и не из сообщения


def test_render_board_respects_budget_and_marks_findings():
    b = EvidenceBoard()
    b.reset("q")
    s = b.add_source("https://x.example", "X", "s" * 500)
    s.opened = True
    for i in range(200):
        b.add_finding(f"вывод номер {i} с уникальным текстом", [s.sid])
    text = b.render_board()
    assert "=== ДОСКА ИССЛЕДОВАНИЯ ===" in text
    assert "[F1]" in text
    assert len(text) <= BOARD_RENDER_MAX_CHARS + 40


def test_render_for_answer_keeps_labels_consistent_with_board():
    b = EvidenceBoard()
    b.reset("q")
    s1 = b.add_source("https://one.example", "One")
    b.set_excerpt(s1.sid, "Текст открытой страницы.")
    b.add_source("https://two.example", "Two", "сниппет неоткрытой")
    b.add_finding("Факт один", [s1.sid])
    out = b.render_for_answer()
    assert "[S1] One" in out and "Факт один" in out and "[S2] Two" in out
    entry = b.as_search_entry()
    assert [s["url"] for s in entry["sources"]] == ["https://one.example", "https://two.example"]


def test_apply_analysis_drops_unknown_sources_and_opened_pages():
    b = EvidenceBoard()
    b.reset("q")
    b.add_source("https://one.example")
    b.add_source("https://two.example")
    b.set_excerpt("S1", "уже открыта")
    b.apply_analysis({"findings": [{"claim": "Что-то", "sources": ["S1", "S9", "memory"]}],
                      "gaps": ["дата"], "open": ["S1", "S2", "S7"], "enough": False})
    assert b.findings[0].sources == ["S1", "memory"]
    assert b.suggested_open == ["S2"]
    assert b.gaps == ["дата"]


# ---------- разбор ответа аналитика ----------

def test_parse_analysis_tolerates_surrounding_text_and_bad_types():
    raw = "Вот мой анализ:\n{\"findings\": [{\"claim\": \"Факт\", \"sources\": \"S1\"}], \"gaps\": \"дата\", \"enough\": true}\nСпасибо"
    res = parse_analysis(raw)
    assert res["findings"] == [{"claim": "Факт", "sources": ["S1"]}]
    assert res["gaps"] == ["дата"]
    assert res["enough"] is True


def test_parse_analysis_returns_none_on_garbage():
    assert parse_analysis("просто текст без json") is None
    assert parse_analysis("{сломанный json") is None
    assert parse_analysis(None) is None


# ---------- многоходовый цикл с настоящими обработчиками поиска ----------

class _FakeController:
    def __init__(self):
        self.evidence = EvidenceBoard()
        self._web_search_results_this_turn = []


async def _run_loop_scenario():
    controller = _FakeController()
    controller.evidence.reset("Какая дата выхода версии 2.0 у проекта Foo?")

    # Поиск: первый вызов даёт S1, уточнение даёт S2.
    search_calls = []

    async def fake_deep_search(q, max_results=3, **kwargs):
        search_calls.append(q)
        if "дата" in q:
            return {"search_performed": True, "sources": [{"url": "https://foo.example/release", "title": "Релиз 2.0"}],
                    "context": "Источник: Релиз 2.0\nURL: https://foo.example/release\nВерсия 2.0 вышла 3 марта."}
        return {"search_performed": True, "sources": [{"url": "https://foo.example/news", "title": "Новости Foo"}],
                "context": "Источник: Новости Foo\nURL: https://foo.example/news\nFoo готовит версию 2.0."}

    async def fake_fetch_url(url, max_chars=None):
        return {"url": url, "title": url, "text": "Полный текст страницы. Версия 2.0 вышла 3 марта.", "ok": True}

    orig_ds, orig_fu = internal_tools.deep_search, internal_tools.fetch_url
    internal_tools.deep_search, internal_tools.fetch_url = fake_deep_search, fake_fetch_url
    try:
        registry = ToolRegistry()
        internal_tools._register_search(registry, controller)

        # Сценарий ответов текстовой модели: решения инструментов и ответы аналитика.
        decisions = [
            {"action": "call_tool", "tool": "internal__web_search", "arguments": {"queries": ["Foo 2.0 новости", "Foo релиз"]}},
            {"action": "call_tool", "tool": "internal__open_page", "arguments": {"sources": ["S1"], "focus": "дата выхода"}},
            {"action": "call_tool", "tool": "internal__web_search", "arguments": {"queries": ["дата выхода Foo 2.0"]}},
            {"action": "call_tool", "tool": "internal__pin_finding", "arguments": {"claim": "Версия 2.0 вышла 3 марта", "sources": ["S1"]}},
        ]
        analyses = [
            {"findings": [], "gaps": ["точная дата выхода"], "open": ["S1"], "enough": False},
            {"findings": [{"claim": "Foo готовит версию 2.0", "sources": ["S2"]}], "gaps": ["дата выхода"], "open": [], "enough": False},
            {"findings": [{"claim": "Версия 2.0 вышла 3 марта", "sources": ["S1"]}], "gaps": [], "open": [], "enough": True},
        ]
        prompts_seen = []

        async def fake_text(messages, temp=0.0, max_tokens=None):
            prompt = messages[0]["content"]
            prompts_seen.append(prompt)
            if "Ты — аналитик" in prompt:
                return "```json\n" + json.dumps(analyses.pop(0), ensure_ascii=False) + "\n```"
            return json.dumps(decisions.pop(0), ensure_ascii=False)

        async def fake_raw(*a, **k):
            raise AssertionError("native-протокол в этом тесте не нужен")

        router = ToolRouter(registry, fake_raw, fake_text)
        router._native_supported = False
        router._native_confirmed = False
        router.board = controller.evidence
        events = []

        async def on_event(ev):
            events.append(ev)

        result = await router.run("Какая дата выхода версии 2.0 у проекта Foo?",
                                  [{"role": "system", "content": "sys"},
                                   {"role": "user", "content": "Какая дата выхода версии 2.0 у проекта Foo?"}],
                                  on_event=on_event)
        return result, controller, search_calls, prompts_seen, events
    finally:
        internal_tools.deep_search, internal_tools.fetch_url = orig_ds, orig_fu


def test_multistep_loop_accumulates_board_and_stops_on_analyst():
    result, controller, search_calls, prompts_seen, events = asyncio.run(_run_loop_scenario())
    board = controller.evidence

    # 1) широкий поиск, 2) открытие страницы, 3) уточняющий поиск, 4) закрепление, затем аналитик «достаточно»
    names = [t["tool"] for t in result["tool_trace"]]
    assert names == ["internal__web_search", "internal__open_page", "internal__web_search"], names
    # после третьего раунда аналитик решил, что данных достаточно: четвёртый вызов не нужен
    assert result["stop_reason"] == "analyst_enough"

    # источники накапливаются, а не затираются между раундами
    assert set(board.sources) == {"S1", "S2"}
    # страница S1 открыта целиком и её фрагмент попал на доску
    assert board.sources["S1"].opened and "3 марта" in board.sources["S1"].excerpt
    # выводы аналитика и закреплённый вывод модели сохранены
    claims = [f.claim for f in board.findings]
    assert "Foo готовит версию 2.0" in claims and "Версия 2.0 вышла 3 марта" in claims
    # закрепление вывода моделью через обработчик pin_finding
    pin = ToolRegistry()
    internal_tools._register_search(pin, controller)
    out = asyncio.run(pin.get("internal__pin_finding").handler({"claim": "Проверено вручную", "sources": ["S2"]}))
    assert "Закреплено как" in out and any(f.claim == "Проверено вручную" for f in board.findings)

    # доска дошла до текстовой модели в решающих промптах
    decision_prompts = [p for p in prompts_seen if "Ты — аналитик" not in p]
    assert any("ДОСКА ИССЛЕДОВАНИЯ" in p and "[S1]" in p for p in decision_prompts[1:])

    # результат финального ответа содержит фрагмент открытой страницы и выводы
    entry = controller._web_search_results_this_turn[0]
    assert "Версия 2.0 вышла 3 марта" in entry["context"]
    assert any(e["kind"] == "analyze" for e in events)


def test_open_page_refuses_urls_not_from_search():
    controller = _FakeController()
    controller.evidence.reset("вопрос без ссылок")
    registry = ToolRegistry()
    internal_tools._register_search(registry, controller)
    spec = registry.get("internal__open_page")
    out = asyncio.run(spec.handler({"sources": ["http://169.254.169.254/latest/meta-data"]}))
    assert "нельзя" in out


def test_memory_results_are_pinned_and_rendered():
    from GCN.tool_router import _is_memory_tool
    assert _is_memory_tool("internal__recall") and _is_memory_tool("internal__memory_search")
    assert not _is_memory_tool("internal__web_search")
    b = EvidenceBoard()
    b.reset("q")
    b.add_memory("- Пользователь живёт в Амстердаме (уверенность: 0.9)", tool="recall")
    b.add_memory("- Пользователь живёт в Амстердаме (уверенность: 0.9)", tool="recall")   # дубликат
    b.add_memory("Ничего не найдено.", tool="recall")                                       # пустой
    assert len(b.memory_notes) == 1
    assert not b.is_empty
    assert "Из памяти (закреплено)" in b.render_board()
    assert "Данные из памяти" in b.render_for_answer()


def test_valid_sids_drops_invented_ids_and_keeps_labels():
    b = EvidenceBoard()
    b.reset("q")
    b.add_source("https://a.example")
    assert b.valid_sids(["S1", "S9", "F3", "memory", "s1"]) == ["S1", "memory"]


def test_board_from_forced_search_is_visible_to_first_decision():
    """Форсированный поиск заполнил доску до ReAct: первое решение модели должно её видеть."""
    controller = _FakeController()
    controller.evidence.reset("Что нового у Foo?")
    src = controller.evidence.add_source("https://foo.example/a", "Статья A", "сниппет A")
    controller.evidence.add_finding("Foo выпустила обновление", [src.sid])
    registry = ToolRegistry()
    internal_tools._register_search(registry, controller)
    seen = []

    async def fake_text(messages, temp=0.0, max_tokens=None):
        seen.append(messages[0]["content"])
        return json.dumps({"action": "stop"}, ensure_ascii=False)

    async def fake_raw(*a, **k):
        raise AssertionError("native не используется")

    router = ToolRouter(registry, fake_raw, fake_text)
    router._native_supported = False
    router._native_confirmed = False
    router.board = controller.evidence
    res = asyncio.run(router.run("Что нового у Foo?", [{"role": "user", "content": "Что нового у Foo?"}]))
    assert res["tool_trace"] == []
    assert seen and "ДОСКА ИССЛЕДОВАНИЯ" in seen[0] and "Foo выпустила обновление" in seen[0] and "[S1]" in seen[0]


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
