import ast, logging, re, textwrap, time, os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Set

# ---- 1. динамические бюджеты (ai_assistant) ----
src = open("/home/claude/project/routes/ai_assistant.py", encoding="utf-8").read().replace("\r\n", "\n")
a = src.index("# ===== Контекст модели"); b = src.index("SELF_TOPIC_RE = re.compile(")
ns = {"os": os, "re": re, "logger": logging.getLogger("t")}
exec(src[a:b], ns)
alloc = ns["_allocate_blocks"]; est = ns["est_tokens"]
facts = "\n".join(f"- факт {i} " + "я" * 200 for i in range(400))
concepts = "\n".join(f"- концепт {i} " + "к" * 200 for i in range(50))
search = "\n".join(f"строка поиска {i} " + "с" * 300 for i in range(400))
# короткие факты и концепты: поиск получает остаток, но не больше своего предела
f1, c1, s1 = alloc("- один факт", "", search, fixed_tokens=1000)
assert f1 == "- один факт" and c1 == ""
assert est(s1) <= ns["CONTEXT_SEARCH_BUDGET_TOKENS"] + 10
assert est(s1) > ns["CONTEXT_SEARCH_BUDGET_TOKENS"] // 2   # поиск получил много
# большие факты: поиск получает то, что осталось
f2, c2, s2 = alloc(facts, concepts, search, fixed_tokens=ns["CONTEXT_INPUT_BUDGET_TOKENS"] - 4000)
total = est(f2) + est(c2) + est(s2)
assert total <= 4000 + 20, total
assert est(f2) <= ns["CONTEXT_FACTS_BUDGET_TOKENS"] + 10
# фиксированная часть больше бюджета — ничего не остаётся, но без ошибок
f3, c3, s3 = alloc(facts, concepts, search, fixed_tokens=10**6)
assert f3 == "" and c3 == "" and s3 == "" or est(f3) + est(c3) + est(s3) == 0
ns["CONTEXT_TOKEN_BUDGET_ENABLED"] = False
assert alloc(facts, concepts, search, 10**6) == (facts, concepts, search)
ns["CONTEXT_TOKEN_BUDGET_ENABLED"] = True
print("1 ok: остаток бюджета перераспределяется, пределы блоков соблюдаются")

# ---- 2. графовое расширение (memory_graph) ----
msrc = open("/home/claude/project/GCN/memory_graph.py", encoding="utf-8").read().replace("\r\n", "\n")
tree = ast.parse(msrc)
fn_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "CognitiveMemory":
        for f in node.body:
            if isinstance(f, ast.FunctionDef) and f.name == "_graph_expansions":
                fn_src = ast.get_source_segment(msrc, f)
assert fn_src
@dataclass
class Fact:
    gcn_id: Optional[str]
@dataclass
class Obj:
    id: str; subject: str; type: str; object: dict; created: Any; confidence: float; scope: Any
class Kt: CLAIM = "claim"
class Sc: 
    def __init__(self, v): self.value = v
class Created:
    def timestamp(self): return 1.0
class Store:
    def __init__(self, objs): self.objs = objs
    def get(self, oid): return self.objs.get(oid)
G = {"List": List, "Dict": Dict, "KnowledgeType": Kt, "HYBRID_WEIGHT_GRAPH": 0.3, "MEMORY_MIN_RELEVANCE": 0.25,
     "GRAPH_EXPANSION_MAX": 2, "GRAPH_EXPANSION_MIN_ACTIVATION": 0.15,
     "GRAPH_EXPANSION_RELEVANCE": 0.30}
exec("class S:\n" + textwrap.indent(fn_src, "    "), G)
S = G["S"]
objs = {f"g{i}": Obj(f"g{i}", f"факт {i}", "claim", {"importance": 1.0}, Created(), 0.8, Sc("private")) for i in range(1, 6)}
class O(S):
    def __init__(self):
        self.facts_by_id = {1: Fact("g1"), 2: Fact("g2"), 3: Fact("g3"), 4: Fact("g4"), 5: Fact("g5"), 9: Fact(None)}
        self.gcn_store = Store(objs)
o = O()
amap = {1: 1.0, 2: 0.4, 3: 0.3, 4: 0.05, 5: 0.2, 9: 0.9}
out = o._graph_expansions(have_gcn_ids={"g3"}, activation_map=amap, exclude={1})
ids = [x["gcn_id"] for x in out]
assert ids == ["g2", "g5"], ids                  # seed 1 исключён, g3 уже есть, g4 ниже порога, 9 без gcn_id
assert len(out) <= 2
assert all(x["via"] == "graph" and x["relevance"] >= 0.25 for x in out)
print("2 ok: графовое расширение: порог, лимит, исключение seed и уже найденных")

# ---- 3. ограничение эндогенных целей (cognition) ----
csrc = open("/home/claude/project/GCN/cognition.py", encoding="utf-8").read().replace("\r\n", "\n")
ctree = ast.parse(csrc)
tick_src = None; helper_src = None; consts = {}
for node in ast.walk(ctree):
    if isinstance(node, ast.ClassDef) and node.name == "MotivationEngine":
        for f in node.body:
            if isinstance(f, ast.FunctionDef) and f.name == "tick":
                tick_src = ast.get_source_segment(csrc, f)
    if isinstance(node, ast.FunctionDef) and node.name == "_is_endogenous_source":
        helper_src = ast.get_source_segment(csrc, node)
    if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "ENDOGENOUS_GOAL_SOURCES" for t in node.targets):
        consts["ENDOGENOUS_GOAL_SOURCES"] = ast.get_source_segment(csrc, node).split("=", 1)[1]
assert tick_src and helper_src
CG = {"Optional": Optional, "Dict": Dict, "Any": Any, "time": time, "logger": logging.getLogger("t"), "MOTIVATION_MAX_ACTIVE": 3, "MOTIVATION_MAX_PER_DAY": 4,
      "ENDOGENOUS_GOAL_SOURCES": eval(consts["ENDOGENOUS_GOAL_SOURCES"])}
exec(helper_src, CG)
exec("class M:\n" + textwrap.indent(tick_src, "    "), CG)
M = CG["M"]
class SM:
    def __init__(self, goals): self.active_goals = goals; self.added = []
    def add_goal(self, goal, priority=0.5, source="external"):
        self.added.append(source); self.active_goals.append({"goal": goal, "source": source, "added_at": time.time()})
class Eng(M):
    def __init__(self, sm): self.self_model = sm; self.gen_calls = 0
    def generate_endogenous_goal(self):
        self.gen_calls += 1
        return {"goal": f"цель {self.gen_calls}", "priority": 0.5, "source": "quality_improvement"}
now = time.time()
# лимит одновременных
sm = SM([{"goal": f"x{i}", "source": "gap_stalled_goal", "added_at": now} for i in range(3)])
e = Eng(sm); assert e.tick() is None and e.gen_calls == 0
# пользовательские и декомпозиционные цели не считаются эндогенными
sm = SM([{"goal": f"u{i}", "source": "external", "added_at": now} for i in range(5)])
e = Eng(sm); assert e.tick() is not None
# старые (3 дня) эндогенные цели в списке активных: лимит одновременности считает их как активные
sm = SM([{"goal": f"d{i}", "source": "identity_stale", "added_at": now - 3 * 86400} for i in range(5)])
e = Eng(sm); assert e.tick() is None
# два старых активных — журнал за сутки пуст, значит можно
sm = SM([{"goal": f"d{i}", "source": "identity_stale", "added_at": now - 3 * 86400} for i in range(2)])
e = Eng(sm); assert e.tick() is not None
# дневной лимит по журналу: цели уже выполнены и не в списке, но за сутки добавлено MAX_PER_DAY
sm = SM([])
e = Eng(sm); e._endo_added_log = [now - 10] * 4
assert e.tick() is None and e.gen_calls == 0
# старые записи журнала (> суток) не блокируют
e = Eng(SM([])); e._endo_added_log = [now - 2 * 86400] * 4
assert e.tick() is not None
# успешное добавление пишет в журнал
sm = SM([]); e = Eng(sm); e.tick()
assert len(e._endo_added_log) == 1
print("3 ok: эндогенные цели ограничены по одновременности и по суткам; пользовательские не считаются")
print("ROUND3 ALL OK")
