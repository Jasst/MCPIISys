import ast, asyncio, hashlib, json, logging, os, re, tempfile, textwrap, time, uuid
from pathlib import Path

src = open("routes/ai_assistant.py", encoding="utf-8").read().replace("\r\n", "\n")
start = src.index("# ===== Контекст модели"); end = src.index("SELF_TOPIC_RE = re.compile(")
calls = {"n": 0, "fail": False}
async def fake_call_llm(messages, temp=0.7, max_tokens=0, **kw):
    calls["n"] += 1
    calls["last_prompt"] = messages[0]["content"]
    if calls["fail"]:
        return ""
    return f"Резюме v{calls['n']}: пользователь обсуждал тему номер {calls['n']} и решил остановиться на ней."
ns = {"os": os, "re": re, "json": json, "uuid": uuid, "asyncio": asyncio, "hashlib": hashlib,
      "logger": logging.getLogger("t"), "call_llm": fake_call_llm}
exec(src[start:end], ns)

# методы класса из исходника
tree = ast.parse(src)
want = {"_summary_path","_ensure_summary_loaded","_compose_history_summary","_refresh_history_summary","_save_summary"}
cls_src = []
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "AIAssistant" or (isinstance(node, ast.ClassDef) and any(
            isinstance(f, ast.AsyncFunctionDef) and f.name == "_refresh_history_summary" for f in node.body)):
        for fn in node.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in want:
                cls_src.append(textwrap.indent(ast.get_source_segment(src, fn), "    "))
        break
# атрибут класса _SUMMARY_PROMPT
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and any(isinstance(f, ast.Assign) and any(getattr(t,'id',None)=="_SUMMARY_PROMPT" for t in f.targets) for f in node.body):
        for f in node.body:
            if isinstance(f, ast.Assign) and any(getattr(t,'id',None)=="_SUMMARY_PROMPT" for t in f.targets):
                cls_src.insert(0, "    _SUMMARY_PROMPT = " + ast.get_source_segment(src, f.value).replace("\n", " "))
        break
assert len(cls_src) == 6, len(cls_src)
G = dict(ns)
exec("class Stub:\n" + "\n".join(cls_src), G)
Stub = G["Stub"]
ns.update(_split_history_by_budget=ns["_split_history_by_budget"])

tmp = Path(tempfile.mkdtemp())
class A(Stub):
    max_history = 40
    def __init__(self):
        self.user_dir = tmp
        self.history = []
        self._active_stream = None
        self.budget_ok = True
    def _consume_autonomous_llm_budget(self, n=1):
        return self.budget_ok
    async def _noop(self): pass

def add(a, n, prefix="реплика"):
    for i in range(n):
        a.history.append({"role": "user", "content": f"{prefix} u{len(a.history)} " + "слово " * 80})
        a.history.append({"role": "assistant", "content": f"{prefix} а{len(a.history)} " + "ответ " * 80})

a = A()
# 1. история короткая — сводки нет, LLM не зовём
add(a, 2)
asyncio.run(a._refresh_history_summary())
assert calls["n"] == 0 and a._summary_anchor is None
print("1 ok: короткая история — LLM не вызывается")

# 2. история длиннее бюджета — есть старые реплики, LLM сворачивает
add(a, 20)
asyncio.run(a._refresh_history_summary())
assert calls["n"] == 1 and a._summary_anchor and a.history_summary.startswith("Резюме v1")
assert (tmp / "history_summary.json").exists()
print("2 ok: LLM-резюме создано, якорь и файл сохранены")

# 3. повторный вызов без новых реплик — ничего не делаем
asyncio.run(a._refresh_history_summary())
assert calls["n"] == 1
print("3 ok: без новых реплик LLM не вызывается повторно")

# 4. компоновка: LLM-резюме + извлечённые реплики после якоря
from_hist = [m for m in a.history[-a.max_history:] if m.get("role") in ("user","assistant")]
_, older = ns["_split_history_by_budget"](from_hist, 3500)
add(a, 2, prefix="новая")
from_hist = [m for m in a.history[-a.max_history:] if m.get("role") in ("user","assistant")]
_, older2 = ns["_split_history_by_budget"](from_hist, 3500)
comp = a._compose_history_summary(older2)
idx = [i for i,m in enumerate(older2) if ns["_msg_fp"](m) == a._summary_anchor][0]
tail = older2[idx+1:]
assert comp.startswith("Резюме v")
if tail:
    assert tail[-1]["content"][:25] in comp
print("4 ok: сводка = LLM-резюме + новые реплики, не учтённые в нём")

# 5. накопление: вызов LLM только если набралась пачка >= SUMMARY_MIN_CHUNK
def chunk_size(obj):
    fr = [m for m in obj.history[-obj.max_history:] if m.get("role") in ("user","assistant")]
    _, ol = ns["_split_history_by_budget"](fr, 3500)
    if not obj._summary_anchor:
        return len(ol)
    idx = [i for i,m in enumerate(ol) if ns["_msg_fp"](m) == obj._summary_anchor]
    return len(ol) - (idx[0] + 1 if idx else 0)
before = calls["n"]
expected = 1 if chunk_size(a) >= ns["SUMMARY_MIN_CHUNK"] else 0
asyncio.run(a._refresh_history_summary())
assert calls["n"] - before == expected, (calls["n"] - before, expected)
add(a, 6, prefix="дальше")
before = calls["n"]
asyncio.run(a._refresh_history_summary())
assert calls["n"] == before + 1
assert "Текущее резюме:" in calls["last_prompt"] and "Резюме v" in calls["last_prompt"]
print("5 ok: обновление пачками (порог SUMMARY_MIN_CHUNK); прошлое резюме передаётся в промпт")

# 6. сбой LLM — прежнее резюме сохраняется
old = a.history_summary; anchor = a._summary_anchor
calls["fail"] = True
add(a, 6, prefix="сбой")
asyncio.run(a._refresh_history_summary())
assert a.history_summary == old and a._summary_anchor == anchor
calls["fail"] = False
print("6 ok: при сбое LLM прежнее резюме и якорь не портятся")

# 7. перезагрузка с диска
b = A(); b.history = a.history
comp_b = b._compose_history_summary(older2)
assert comp_b.startswith("Резюме v")
print("7 ok: резюме переживает перезапуск (читается с диска)")

# 8. суточный бюджет исчерпан — пропуск
c = A(); c.history = a.history; c.budget_ok = False
before = calls["n"]
asyncio.run(c._refresh_history_summary())
assert calls["n"] == before
print("8 ok: при исчерпанном бюджете фонового LLM обновления нет")
print("ALL OK")
