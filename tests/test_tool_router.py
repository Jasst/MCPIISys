import asyncio, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/claude/proj")
import GCN.tool_router as tr

tr._NATIVE_FLAG_PATH = Path(tempfile.mkdtemp()) / "flag.json"
tr.MAX_TOOL_ITERATIONS = 3

def tc(name, args, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}

def make_registry(calls, search_result="Найдено: 3 статьи о погоде"):
    reg = tr.ToolRegistry()
    async def search(args):
        calls.append(("search", dict(args)))
        return search_result(args) if callable(search_result) else search_result
    async def recall(args):
        calls.append(("recall", dict(args)))
        return "Ничего не найдено по запросу"   # нормальный пустой ответ, не ошибка-префикс
    reg.register("web_search", "search web", {"type": "object", "properties": {}}, search)
    reg.register("recall", "recall memory", {"type": "object", "properties": {}}, recall)
    return reg

def scripted(seq):
    seq = list(seq); seen = []
    async def raw(messages, temp=0.0, max_tokens=0, tools=None):
        seen.append([dict(m) for m in messages])
        return seq.pop(0) if seq else {}
    return raw, seen

async def fb_none(*a, **k): return None
async def fb_no_tool(*a, **k): return {"action": "no_tool"}

async def case_A_native_flow():
    calls = []
    raw, seen = scripted([
        {"content": "", "tool_calls": [tc("internal__web_search", {"query": "Погода"}, "c1")]},
        {"content": "готово", "tool_calls": None},
    ])
    r = tr.ToolRouter(make_registry(calls), raw, fb_none)
    out = await r.run("какая погода", [{"role": "user", "content": "какая погода"}])
    assert out["used_native"] is True
    assert len(calls) == 1
    assert json.loads(tr._NATIVE_FLAG_PATH.read_text())["native_supported"] is True
    # второй раунд видит протокол: assistant(tool_calls) + tool(tool_call_id)
    last = seen[1]
    roles = [m["role"] for m in last]
    assert roles[-2:] == ["assistant", "tool"], roles
    assert last[-1]["tool_call_id"] == "c1"
    print("A ok: native flow, tool message protocol, flag saved True")

async def case_B_transient_error_does_not_poison():
    calls = []
    raw, _ = scripted([{}, {}])          # два пустых ответа = сбой
    r = tr.ToolRouter(make_registry(calls), raw, fb_none)
    await r.run("погода", [{"role": "user", "content": "погода"}])
    assert r._native_supported is None, r._native_supported
    assert not tr._NATIVE_FLAG_PATH.exists() or json.loads(tr._NATIVE_FLAG_PATH.read_text()).get("native_supported") is not False
    # следующий запрос снова пробует нативный режим
    raw2, seen2 = scripted([{"content": "", "tool_calls": [tc("internal__recall", {"query": "x"}, "c2")]}, {"content": "ok"}])
    r.llm_raw_caller = raw2
    out = await r.run("вспомни x", [{"role": "user", "content": "вспомни x"}])
    assert out["used_native"] is True
    print("B ok: один сбой не отключает нативный режим")

async def case_C_old_false_flag_ignored():
    tr._NATIVE_FLAG_PATH.write_text(json.dumps({"native_supported": False}))
    raw, _ = scripted([])
    r = tr.ToolRouter(make_registry([]), raw, fb_none)
    assert r._native_supported is None
    print("C ok: старый файл с False игнорируется (миграция)")

async def case_D_duplicates_and_case():
    calls = []
    raw, seen = scripted([
        {"content": "", "tool_calls": [tc("internal__web_search", {"query": "Погода"}, "d1")]},
        {"content": "", "tool_calls": [tc("internal__web_search", {"query": "погода"}, "d2")]},  # повтор, другой регистр
        {"content": "итог", "tool_calls": None},
    ])
    r = tr.ToolRouter(make_registry(calls), raw, fb_none)
    out = await r.run("погода", [{"role": "user", "content": "погода"}])
    assert len(calls) == 1, calls
    assert len(out["tool_trace"]) == 1
    # раунд из одних повторов завершает цикл: третий LLM-вызов не делается
    assert len(seen) == 2, len(seen)
    assert len(out["tool_trace"]) == 1
    print("D ok: повтор с другим регистром не выполняется, tool_call_id совпадает")

async def case_E_error_detection():
    # 1) слово «ошибка» внутри нормального результата — НЕ ошибка, рефлексии нет
    calls = []
    reflect = []
    raw, _ = scripted([
        {"content": "", "tool_calls": [tc("internal__web_search", {"query": "a"}, "e1")]},
        {"content": "ok"},
    ])
    reg = make_registry(calls, search_result="В статье описана типичная ошибка в налоговой отчётности")
    r = tr.ToolRouter(reg, raw, fb_none)
    async def spy(*a, **k):
        reflect.append(1); return {"next_action": "give_up"}
    r._reflect_on_failure = spy
    out = await r.run("q", [{"role": "user", "content": "q"}])
    assert out["tool_trace"][0]["ok"] is True and reflect == []
    # 2) настоящая ошибка с префиксом — рефлексия вызывается
    calls2 = []
    raw2, _ = scripted([
        {"content": "", "tool_calls": [tc("internal__web_search", {"query": "b"}, "e2")]},
        {"content": "ok"},
    ])
    reg2 = make_registry(calls2, search_result="Ошибка: сервис недоступен")
    r2 = tr.ToolRouter(reg2, raw2, fb_none)
    r2._reflect_on_failure = spy
    out2 = await r2.run("q", [{"role": "user", "content": "q"}])
    assert out2["tool_trace"][0]["ok"] is False and reflect == [1]
    print("E ok: 'ошибка' внутри текста не считается сбоем; настоящий сбой — считается")

async def case_F_replan_extends_loop():
    calls = []
    def fail(args): return "Ошибка: временный сбой"
    seq = []
    for i in range(6):
        seq.append({"content": "", "tool_calls": [tc("internal__web_search", {"query": f"q{i}"}, f"f{i}")]})
    seq.append({"content": "итог"})
    raw, _ = scripted(seq)
    reg = make_registry(calls, search_result=fail)
    r = tr.ToolRouter(reg, raw, fb_none)
    async def replan(*a, **k):
        return {"next_action": "replan", "new_plan": "попробовать иначе"}
    r._reflect_on_failure = replan
    out = await r.run("q", [{"role": "user", "content": "q"}])
    assert len(out["tool_trace"]) > tr.MAX_TOOL_ITERATIONS, len(out["tool_trace"])
    print(f"F ok: перепланирование расширяет цикл ({len(out['tool_trace'])} вызовов при базовом лимите {tr.MAX_TOOL_ITERATIONS})")

async def case_G_fallback_path_still_works():
    calls = []
    async def fb(*a, **k):
        if fb.n == 0:
            fb.n += 1; return {"action": "call_tool", "tool": "internal__web_search", "arguments": {"query": "z"}}
        return None
    fb.n = 0
    raw, _ = scripted([{}])  # native не работает
    r = tr.ToolRouter(make_registry(calls), raw, fb_none)
    r._decide_fallback = fb
    out = await r.run("z", [{"role": "user", "content": "z"}])
    assert out["used_native"] is False and len(calls) == 1
    print("G ok: текстовый fallback работает как раньше")

async def main():
    for c in [case_A_native_flow, case_B_transient_error_does_not_poison, case_C_old_false_flag_ignored,
              case_D_duplicates_and_case, case_E_error_detection, case_F_replan_extends_loop,
              case_G_fallback_path_still_works]:
        tr._NATIVE_FLAG_PATH.unlink(missing_ok=True)
        await c()
    print("ALL PASSED")

asyncio.run(main())
