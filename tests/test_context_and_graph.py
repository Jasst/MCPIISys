import ast, os, re, time, types, logging
from typing import List, Dict
from dataclasses import dataclass, field

# ---------- 1. helpers из routes/ai_assistant.py ----------
src = open("routes/ai_assistant.py", encoding="utf-8").read().replace("\r\n", "\n")
start = src.index("# ===== Контекст модели")
end = src.index("SELF_TOPIC_RE = re.compile(")
ns = {"os": os, "re": re, "logger": logging.getLogger("t")}
exec(src[start:end], ns)
est, trim_lines, trim_text = ns["est_tokens"], ns["_trim_lines"], ns["_trim_text"]
split, summ, wm_u, wrap = ns["_split_history_by_budget"], ns["_extractive_summary"], ns["_wm_unique"], ns["_wrap"]

assert est("abc") == 1 and est("") == 0
txt = "\n".join(f"строка {i} " + "x" * 30 for i in range(100))
trimmed = trim_lines(txt, 200)
assert est(trimmed) <= 200 + 2 and trimmed.split("\n")[0] == txt.split("\n")[0]
assert trim_text("a" * 10000, 100).startswith("a") and len(trim_text("a" * 10000, 100)) < 400
hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i} " + "y" * 400} for i in range(30)]
recent, older = split(hist, 1000)
assert recent[-1] is hist[-1] and len(recent) + len(older) == 30 and len(recent) < 30
assert older and older[-1] is hist[len(older) - 1]
s = summ(older)
assert s.startswith("- ") and len(s) <= 1200 and "msg" in s and "msg0 " not in s
assert wm_u(["Анна живёт в Гааге и любит кофе", "совсем новая тема про сеть"], "- [личный факт] Анна живёт в Гааге и любит кофе") == ["совсем новая тема про сеть"]
assert wrap("memory", "X", "H").startswith("<memory>")
print("1 ok: бюджет, обрезка, сжатие истории, дедупликация рабочей памяти")

# ---------- 2. граф: затухание и усиление ----------
gsrc = open("GCN/memory_graph.py", encoding="utf-8").read().replace("\r\n", "\n")
tree = ast.parse(gsrc)
methods = {}
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "CognitiveMemory":
        for fn in node.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in (
                    "_apply_decay", "reinforce_coactivation", "spread_activation"):
                methods[fn.name] = ast.get_source_segment(gsrc, fn)
assert len(methods) == 3, methods.keys()
import textwrap
G = dict(time=time, List=List, Dict=Dict, defaultdict=__import__("collections").defaultdict,
         Optional=None, Tuple=None, logger=logging.getLogger("t"),
         SYNAPSE_DECAY_RATE=0.001, SYNAPSE_MIN_WEIGHT=0.01, SYNAPSE_MAX_WEIGHT=1.0,
         SYNAPSE_INITIAL_WEIGHT=0.1, SPREADING_DECAY=0.5, SPREADING_THRESHOLD=0.05,
         HEBBIAN_LEARNING_RATE=0.02)
from typing import Optional, Tuple
G.update(Optional=Optional, Tuple=Tuple)
code = "class Stub:\n" + "\n".join(textwrap.indent(methods[k], "    ") for k in
                                 ("_apply_decay", "reinforce_coactivation"))
exec(code, G)
Stub = G["Stub"]

@dataclass
class Syn:
    weight: float = 0.1; last_activation: float = 0.0; last_coactivation: float = 0.0
    coactivation_count: int = 0; confidence: float = 0.5; plasticity: float = 1.0

class S(Stub):
    def __init__(self):
        self.synapses = {(1, 2): Syn(weight=0.5, last_activation=time.time() - 10 * 86400),
                         (2, 1): Syn(weight=0.5, last_activation=time.time() - 10 * 86400)}
        self.facts_by_id = {1: object(), 2: object(), 3: object()}
        self._dirty = False
    def _sync_synapse_to_gcn(self, a, b): pass
    def _create_synapse(self, src, tgt, weight=0.1):
        self.synapses[(src, tgt)] = Syn(weight=weight, last_activation=time.time())

o = S()
t0 = time.time()
o._last_decay_ts = t0 - 86400          # прошли сутки с прошлого прохода
o._apply_decay()
w_one_day = o.synapses[(1, 2)].weight
assert abs(w_one_day - 0.5 * (1 - 0.001)) < 1e-9, w_one_day
o._apply_decay()                        # сразу второй проход — без повторного затухания
assert abs(o.synapses[(1, 2)].weight - w_one_day) < 1e-9
o2 = S(); o2._last_decay_ts = t0 - 86400 * 2
o2._apply_decay()
assert o2.synapses[(1, 2)].weight < w_one_day
print("2 ok: затухание пропорционально времени, без накопления за повторные проходы")

o = S()
n = o.reinforce_coactivation([1, 2, 3], delta=0.03)
assert n == 6, n                        # 3 пары × 2 направления
assert o.synapses[(1, 2)].weight == min(1.0, 0.5 + 0.03)
assert (3, 1) in o.synapses and (1, 3) in o.synapses
assert o._dirty
print("3 ok: усиление связей между co-активными фактами в обоих направлениях")
