"""Проверка порядка в верификаторе противоречий: NLI до LLM, невалидный JSON отбрасывается.
Метод извлекается из routes/ai_assistant.py (сам модуль требует fastapi и MCP)."""
import asyncio, logging, os, sys, types
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import GCN.json_schema as js
from GCN.nli import NLIJudge
src = open(os.path.join(ROOT, "routes/ai_assistant.py"), encoding="utf-8").read().replace("\r\n", "\n")
i = src.index("    def _nli_label(self, text_a: str, text_b: str)")
j = src.index("\n    async def ", src.index("    async def _verify_pending_contradictions"))
block = "\n".join(l[4:] if l.startswith("    ") else l for l in src[i:j].split("\n"))
# таблица ссылок, которые метод использует
PROMPT = "PROMPT {text_a} {text_b}"
def parse_llm_json(raw):
    import json
    try: return json.loads(raw)
    except Exception: return None
logger = logging.getLogger("t")
ns = {"logger": logger, "parse_llm_json": parse_llm_json, "CONTRADICTION_VERIFY_PROMPT": PROMPT,
      "CONTRADICTION_RESPONSE_FORMAT": js.CONTRADICTION_RESPONSE_FORMAT,
      "CONTRADICTION_VERDICT_SCHEMA": js.CONTRADICTION_VERDICT_SCHEMA, "validate_json": js.validate,
      "NLIJudge": NLIJudge, "Optional": __import__("typing").Optional, "List": __import__("typing").List, "Dict": __import__("typing").Dict, "Any": __import__("typing").Any}
exec(compile("class C:\n" + "\n".join("    " + l for l in block.split("\n")) + "\n", "x", "exec"), ns)
C = ns["C"]

class Fact:
    def __init__(self, i, text, gid): self.id = i; self.text = text; self.gcn_id = gid; self.confidence = 0.5; self.contradicts = set()
class Graph:
    def __init__(self): self.removed = []
    def remove_relation(self, a, r, b): self.removed.append((a, b))
class Store:
    def __init__(self): self._graph = Graph(); self.updated = []
    def update(self, gid, data, actor): self.updated.append(gid)
class Mem:
    def __init__(self, pairs): self._pairs = pairs; self.store = Store()
    async def _schedule_save(self): return None
    def get_unverified_contradictions(self, limit=5): return self._pairs
class Ctl(C):
    def __init__(self, pairs, llm_replies, nli_label):
        self.memory = Mem(pairs); self.user_id = "u"; self._llm_calls = []; self._replies = list(llm_replies)
        self._nli_judge = types.SimpleNamespace(available=True, decide=lambda a, b, t: nli_label)
        self.demoted = []
    async def _bg_llm(self, messages, **kw):
        self._llm_calls.append(kw)
        return self._replies.pop(0)
    def _demote_or_retract(self, fact, actor, reason): self.demoted.append(fact.id)

pair = (Fact(1, "Сервер стоит в Москве", "g1"), Fact(2, "Сервер стоит в Берлине", "g2"))

# 1) NLI говорит neutral -> LLM не вызывается, рёбра снимаются
c = Ctl([pair], [], nli_label="neutral")
asyncio.run(c._verify_pending_contradictions())
assert c._llm_calls == [], "LLM не должна вызываться при NLI-решении"
assert c.memory.store._graph.removed, "ложное противоречие должно быть снято"
print("NLI-first: LLM calls =", len(c._llm_calls), "edges removed =", len(c.memory.store._graph.removed))

# 2) NLI молчит, LLM отдаёт невалидный по схеме JSON -> ничего не меняем
c = Ctl([pair], ['{"relation": "maybe", "keep": "B", "reason": "x"}'], nli_label=None)
asyncio.run(c._verify_pending_contradictions())
assert len(c._llm_calls) == 1 and c._llm_calls[0].get("response_format") is not None
assert c.demoted == [] and c.memory.store._graph.removed == [], "невалидный ответ не должен менять память"
print("schema reject: no changes, response_format sent =", True)

# 3) NLI молчит, LLM отдаёт валидный вердикт keep=B -> факт A... (keep B => demote A)
c = Ctl([pair], ['{"relation": "true_contradiction", "keep": "B", "reason": "x"}'], nli_label=None)
asyncio.run(c._verify_pending_contradictions())
assert c.demoted == [1], c.demoted
print("valid verdict applied: demoted fact", c.demoted)
print("CONTRADICTION FLOW CHECK PASSED")
