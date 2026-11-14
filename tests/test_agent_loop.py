"""Сценарные тесты агентного цикла: цепочки, защита от необратимых правок, зацикливание,
пропуск шага, ранжирование инструментов, повторный осмотр изображения.
Запуск: python tests/test_agent_loop.py"""
import asyncio
import base64
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import GCN.tool_router as tr
from GCN.tool_router import ToolRegistry, ToolRouter, might_need_tools, rank_tools
from GCN.memory_tools import register_memory_edit_tools
from GCN.vision_tools import crop_image_b64, register_vision_tools

tr._NATIVE_FLAG_PATH = Path(tempfile.mkdtemp()) / "flag.json"


def tc(name, args, cid):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def scripted(seq):
    seq = list(seq)
    seen = []

    async def raw(messages, temp=0.0, max_tokens=0, tools=None):
        seen.append([dict(m) for m in messages])
        return seq.pop(0) if seq else {"content": "", "tool_calls": None}
    return raw, seen


async def no_fallback(*a, **k):
    return None


class FakeMemory:
    """Минимальная имитация MemoryService: нужные методы и их контракт."""

    def __init__(self):
        self.facts = {"fct_aaa111": {"text": "любимый цвет синий", "scope": "private"},
                      "fct_bbb222": {"text": "живу в Амстердаме", "scope": "private"}}
        self.removed = []
        self.added = []

    async def recall(self, query, top_k=5, scope=None, **kw):
        q = query.lower()
        return [{"id": k, "text": v["text"], "scope": v["scope"], "confidence": 0.9}
                for k, v in self.facts.items() if q.split()[0] in v["text"].lower()][:top_k]

    async def explain_fact(self, fid):
        v = self.facts.get(fid)
        if not v:
            return {"error": "не найден"}
        return {"id": fid, "text": v["text"], "scope": v["scope"], "confidence": 0.9, "version": 1}

    async def forget(self, key, scope="private", dry_run=True):
        if key in self.facts:
            if dry_run:
                return {"status": "dry_run", "would_remove": 1,
                        "candidates": [{"id": key, "text": self.facts[key]["text"]}]}
            self.removed.append(key)
            self.facts.pop(key)
            return {"status": "ok", "removed": 1}
        return {"status": "ok", "removed": 0}

    async def remember(self, fact, scope=None, user_explicit=False, **kw):
        fid = f"fct_new{len(self.added)}"
        self.facts[fid] = {"text": fact, "scope": scope or "private"}
        self.added.append(fact)
        return {"id": fid, "scope": scope or "private", "fact": fact}


class FakeController:
    def __init__(self, memory, image=None):
        self.memory_service = memory
        self._image = image

    def current_turn_image(self):
        return self._image


def make_registry(memory):
    reg = ToolRegistry()
    register_memory_edit_tools(reg, FakeController(memory))

    async def web(args):
        return "Найдено: курс 90"
    reg.register("web_search", "search web", {"type": "object", "properties": {}}, web)
    reg.register("recall", "recall", {"type": "object", "properties": {}},
                 lambda a: asyncio.sleep(0, result="Ничего не найдено"))
    return reg


# ---------- 1. Цепочка: найти → предпросмотр → подтвердить ----------
async def scenario_edit_chain():
    mem = FakeMemory()
    reg = make_registry(mem)
    raw, _ = scripted([
        {"content": "", "tool_calls": [tc("internal__memory_search", {"query": "цвет"}, "s1")]},
        {"content": "", "tool_calls": [tc("internal__memory_forget", {"fact_id": "fct_aaa111"}, "f1")]},
        {"content": "", "tool_calls": [tc("internal__memory_forget", {"fact_id": "fct_aaa111", "confirm": True}, "f2")]},
        {"content": "Готово, удалил.", "tool_calls": None},
    ])
    r = ToolRouter(reg, raw, no_fallback)
    out = await r.run("удали факт про любимый цвет", [{"role": "user", "content": "удали факт про любимый цвет"}])
    assert mem.removed == ["fct_aaa111"], mem.removed
    assert [t["tool"] for t in out["tool_trace"]] == [
        "internal__memory_search", "internal__memory_forget", "internal__memory_forget"]
    assert out["tool_trace"][1]["result"].find("preview") != -1, out["tool_trace"][1]["result"]
    assert out["stop_reason"] == "model_done"
    print("1 ok: цепочка поиск → предпросмотр → подтверждение выполнена за один запуск")


# ---------- 2. Защита: удаление без предпросмотра запрещено ----------
async def scenario_guard_no_preview():
    mem = FakeMemory()
    reg = make_registry(mem)
    raw, _ = scripted([
        {"content": "", "tool_calls": [tc("internal__memory_forget", {"fact_id": "fct_bbb222", "confirm": True}, "g1")]},
        {"content": "не удалил", "tool_calls": None},
    ])
    r = ToolRouter(reg, raw, no_fallback)
    out = await r.run("забудь что я живу в Амстердаме", [{"role": "user", "content": "x"}])
    assert mem.removed == [], "удаление без предпросмотра не должно происходить"
    assert out["tool_trace"][0]["ok"] is False
    print("2 ok: удаление с confirm без предпросмотра отклонено, данные целы")


# ---------- 3. Защита: слишком широкий запрос ----------
async def scenario_guard_wide_query():
    mem = FakeMemory()
    reg = make_registry(mem)
    raw, _ = scripted([
        {"content": "", "tool_calls": [tc("internal__memory_forget", {"query": "я"}, "w1")]},
        {"content": "ок", "tool_calls": None},
    ])
    r = ToolRouter(reg, raw, no_fallback)
    out = await r.run("сотри всё про меня", [{"role": "user", "content": "x"}])
    assert out["tool_trace"][0]["ok"] is False and mem.removed == []
    print("3 ok: короткий query не запускает массовое удаление")


# ---------- 4. Зацикливание: модель повторяет тот же вызов в другом регистре ----------
async def scenario_stuck_detection():
    mem = FakeMemory()
    reg = make_registry(mem)
    seq = [{"content": "", "tool_calls": [tc("internal__web_search", {"query": f"Курс {i}"}, f"c{i}")]}
           for i in range(1)]
    seq += [{"content": "", "tool_calls": [tc("internal__web_search", {"query": "курс 1"}, "d1")]},
            {"content": "", "tool_calls": [tc("internal__web_search", {"query": "КУРС 1"}, "d2")]},
            {"content": "", "tool_calls": [tc("internal__web_search", {"query": "курс 1 "}, "d3")]},
            {"content": "итог", "tool_calls": None}]
    raw, seen = scripted(seq)
    r = ToolRouter(reg, raw, no_fallback)
    out = await r.run("курс доллара", [{"role": "user", "content": "x"}])
    assert len([t for t in out["tool_trace"] if t["tool"] == "internal__web_search"]) == 2
    assert out["stop_reason"] in ("no_progress", "stuck"), out["stop_reason"]
    assert len(seen) <= 4, len(seen)
    print(f"4 ok: зацикливание прервано (причина: {out['stop_reason']}, LLM-вызовов: {len(seen)})")


# ---------- 5. Пропуск шага прерывает зависший инструмент ----------
async def scenario_skip():
    reg = ToolRegistry()
    started = asyncio.Event()

    async def slow(args):
        started.set()
        await asyncio.sleep(30)
        return "поздно"
    reg.register("generate_image", "slow", {"type": "object", "properties": {}}, slow, timeout_seconds=60)
    raw, seen = scripted([
        {"content": "", "tool_calls": [tc("internal__generate_image", {"prompt": "кот"}, "k1")]},
        {"content": "ответ без картинки", "tool_calls": None},
    ])
    r = ToolRouter(reg, raw, no_fallback)

    async def skipper():
        await started.wait()
        await asyncio.sleep(0.05)
        r.request_skip()
    t0 = asyncio.get_event_loop().time()
    out, _ = await asyncio.gather(
        r.run("нарисуй кота", [{"role": "user", "content": "x"}]), skipper())
    elapsed = asyncio.get_event_loop().time() - t0
    assert elapsed < 5, f"пропуск должен прерывать ожидание, прошло {elapsed:.1f}с"
    assert out["tool_trace"][0]["skipped"] is True
    assert out["stop_reason"] == "user_skip"
    print(f"5 ok: пропуск прервал инструмент за {elapsed:.2f}с, причина: user_skip")


# ---------- 6. Ранжирование: при большом каталоге модель видит релевантное + core ----------
async def scenario_ranking_offers():
    reg = ToolRegistry()

    async def h(a):
        return "ok"
    names = ["recall", "remember", "web_search", "memory_search", "memory_forget", "memory_update",
             "memory_explain", "generate_image", "inspect_image", "read_code", "search_code",
             "project_structure", "analyze_error", "get_identity_chain", "contribute_to_identity",
             "merge_identity_branches", "list_tools"]
    for n in names:
        reg.register(n, f"описание {n}", {"type": "object", "properties": {}}, h)
    r = ToolRouter(reg, scripted([])[0], no_fallback)
    offered = r._offered_tools("исправь факт в памяти про цвет", reg._visible())
    assert "internal__memory_update" in offered and "internal__memory_search" in offered
    assert "internal__recall" in offered and "internal__web_search" in offered  # core всегда
    assert len(offered) <= tr.TOOL_NATIVE_MAX
    ranked, why = rank_tools("нарисуй кота", reg._visible())
    assert ranked[0] == "internal__generate_image", ranked[:3]
    print(f"6 ok: модель видит {len(offered)} из {len(names)}, core всегда, релевантное первым")


# ---------- 7. Маршрутизация: «нужен ли инструмент» ----------
def scenario_might_need():
    assert might_need_tools("удали из памяти старый адрес")
    assert might_need_tools("что на этой картинке в правом углу")
    assert might_need_tools("исправь факт про работу")
    assert not might_need_tools("спасибо, понятно")
    assert not might_need_tools("как дела")
    print("7 ok: маркеры маршрутизации ловят правки памяти и вопросы о картинке")


# ---------- 8. Осмотр изображения: вырезка и вызов модели с точным вопросом ----------
async def scenario_vision():
    from PIL import Image
    im = Image.new("RGB", (1000, 500), (10, 200, 10))
    buf = io.BytesIO(); im.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    crop = crop_image_b64(b64, [0.0, 0.0, 0.5, 0.5])
    assert Image.open(io.BytesIO(base64.b64decode(crop))).size == (500, 250)

    seen = {}

    async def fake_llm(messages, temp=0.0, max_tokens=0, **kw):
        seen["content"] = messages[0]["content"]
        return "На табличке написано «Открыто»"
    reg = ToolRegistry()
    register_vision_tools(reg, FakeController(None, image=b64), fake_llm)
    res = await reg.get("internal__inspect_image").handler(
        {"question": "что на табличке", "region": [0, 0, 0.5, 0.5]})
    assert res.startswith("На табличке"), res
    img_part = seen["content"][1]["image_url"]["url"]
    assert img_part.startswith("data:image/png;base64,")
    print("8 ok: инспекция вырезает область и отправляет точный вопрос модели")


# ---------- 9. Контекст прошлых ходов доходит до модели ----------
async def scenario_cross_turn_context():
    mem = FakeMemory()
    reg = make_registry(mem)
    raw, seen = scripted([{"content": "Удалил.", "tool_calls": None}])
    r = ToolRouter(reg, raw, no_fallback)
    await r.run("удали то, что я сказал выше", [{"role": "user", "content": "x"}],
                context_notes=["на «цвет»: memory_search → id=fct_aaa111 любимый цвет синий"])
    joined = " ".join(m.get("content", "") for m in seen[0] if isinstance(m.get("content"), str))
    assert "fct_aaa111" in joined, "контекст прошлого хода должен быть в запросе к модели"
    print("9 ok: id из прошлого хода доступен модели без повторного поиска")


# ---------- 10. ask_user останавливает цикл и отдаёт вопрос ----------
async def scenario_ask_user():
    from GCN.agent_tools import register_agent_tools
    mem = FakeMemory()
    reg = make_registry(mem)
    register_agent_tools(reg)
    raw, seen = scripted([
        {"content": "", "tool_calls": [tc("internal__ask_user",
            {"question": "Какой из двух адресов удалить: Амстердам или Казань?"}, "a1")]},
        {"content": "не должен вызываться", "tool_calls": [tc("internal__memory_forget", {"fact_id": "fct_bbb222"}, "x")]},
    ])
    r = ToolRouter(reg, raw, no_fallback)
    out = await r.run("удали мой адрес", [{"role": "user", "content": "x"}])
    assert out["stop_reason"] == "ask_user", out["stop_reason"]
    assert out["pending_question"].startswith("Какой из двух"), out.get("pending_question")
    assert mem.removed == [], "после вопроса ничего не удаляется"
    assert len(seen) == 1, "после вопроса новых LLM-вызовов нет"
    print("10 ok: ask_user отдаёт один вопрос, цикл останавливается, данные целы")


# ---------- 11. Бюджет рефлексии: после исчерпания LLM-диагностика не вызывается ----------
async def scenario_reflection_budget():
    reg = ToolRegistry()
    async def fail(a):
        return "Ошибка: сервис недоступен"
    reg.register("web_search", "search", {"type": "object", "properties": {}}, fail)
    seq = [{"content": "", "tool_calls": [tc("internal__web_search", {"query": f"q{i}"}, f"b{i}")]} for i in range(4)]
    seq.append({"content": "итог", "tool_calls": None})
    raw, _ = scripted(seq)
    r = ToolRouter(reg, raw, no_fallback)
    llm_calls = []
    orig = r._reflect_on_failure
    async def counting(*a, allow_llm=True, **k):
        llm_calls.append(allow_llm)
        return await orig(*a, allow_llm=allow_llm, **k)
    r._reflect_on_failure = counting
    async def fake_text(*a, **k):
        return '{"diagnosis": "x", "next_action": "give_up"}'
    r.llm_text_caller = fake_text
    await r.run("q", [{"role": "user", "content": "q"}])
    assert llm_calls.count(True) == tr.AGENT_REFLECTION_BUDGET, llm_calls
    assert llm_calls.count(False) >= 1, llm_calls
    print(f"11 ok: LLM-рефлексий не больше бюджета ({tr.AGENT_REFLECTION_BUDGET}), остальные без LLM")


async def main():
    await scenario_edit_chain()
    await scenario_guard_no_preview()
    await scenario_guard_wide_query()
    await scenario_stuck_detection()
    await scenario_skip()
    await scenario_ranking_offers()
    scenario_might_need()
    await scenario_vision()
    await scenario_cross_turn_context()
    await scenario_ask_user()
    await scenario_reflection_budget()
    print("ALL AGENT SCENARIOS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
