"""Проверки: связи (противоречие в обе стороны, смена типа), последствия, ответы ИИ, маршруты."""
import asyncio, sys, types, random
sys.path.insert(0, ".")

fake_mg = types.ModuleType("GCN.memory_graph")
fake_llm = types.ModuleType("GCN.llm_client")
sys.modules["GCN.memory_graph"] = fake_mg
sys.modules["GCN.llm_client"] = fake_llm

async def _placeholder_llm(*a, **k):
    return None
fake_llm.call_llm = _placeholder_llm   # нужен до импорта маршрутов
fake_mg.get_memory_service = lambda uid: None  # переопределим ниже

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from GCN.GCN import MemoryStore
from GCN import memory_viewer_core as core
from dependencies import require_auth


def make_store():
    random.seed(2)
    st = MemoryStore(embedding_dim=8)
    a = st.add_fact("Работаю дома", "claim", "u", confidence=0.7)
    b = st.add_fact("Работаю в офисе", "claim", "u", confidence=0.6)
    c = st.add_fact("Есть ноутбук", "claim", "u", confidence=0.8)
    st.register_contradiction(a, b, "u")
    st.link(c, a, "GROUNDS_IN", "u", weight=0.9)
    return st, a, b, c


def test_unlink_contradiction_removes_both_sides():
    st, a, b, _ = make_store()
    core.apply_edit(st, "unlink", {"source": a, "target": b, "relation": "CONTRADICTS"})
    assert not any(r in core.CONTRADICT_RELATIONS for r, _ in st._graph.get_neighbors(a))
    assert not any(r in core.CONTRADICT_RELATIONS for r, _ in st._graph.get_neighbors(b))
    print("ok: unlink CONTRADICTS снимает обе стороны")


def test_link_contradiction_is_bidirectional():
    st = MemoryStore(embedding_dim=8)
    x = st.add_fact("А", "claim", "u"); y = st.add_fact("Б", "claim", "u")
    core.apply_edit(st, "link", {"source": x, "target": y, "relation": "CONTRADICTS"})
    assert any(r == "CONTRADICTS" and t == y for r, t in st._graph.get_neighbors(x))
    assert any(r == "CONTRADICTS" and t == x for r, t in st._graph.get_neighbors(y))
    print("ok: link CONTRADICTS создаёт обе стороны")


def test_edge_replace_keeps_weight_and_validates_type():
    st, a, b, c = make_store()
    core.apply_edit(st, "edge_replace", {"source": c, "target": a, "old_relation": "GROUNDS_IN", "new_relation": "CAUSES"})
    rels = [r for r, t in st._graph.get_neighbors(c) if t == a]
    assert rels == ["CAUSES"], rels
    assert abs(st._graph.get_relation_weight(c, "CAUSES", a) - 0.9) < 1e-6
    for bad in ({"new_relation": "DROP_ALL"}, {"new_relation": "CAUSES", "old_relation": "CAUSES"}):
        args = {"source": c, "target": a, "old_relation": "CAUSES", "new_relation": "CAUSES"} | bad
        try:
            core.apply_edit(st, "edge_replace", args)
            raise AssertionError("должно было отказать")
        except ValueError:
            pass
    print("ok: смена типа сохраняет силу связи, неверный тип отклоняется")


def test_impact_lists_dependencies():
    st, a, b, c = make_store()
    im = core.impact(st, a)
    assert im["relied_by"] == ["Есть ноутбук"], im
    assert im["contradicts"] == ["Работаю в офисе"], im
    im_c = core.impact(st, c)
    assert im_c["relies_on"] == ["Работаю дома"], im_c
    print("ok: последствия: кто держится на записи и от чего она зависит")


def test_llm_answers_are_sanitized():
    p = core.sanitize_edge({"verdict": "change", "new_relation": "drop table", "reason": "x" * 999}, "GROUNDS_IN")
    assert p["verdict"] == "unclear" and p["new_relation"] is None and len(p["reason"]) <= 400
    p = core.sanitize_edge({"verdict": "change", "new_relation": "CAUSES", "extra": 1}, "GROUNDS_IN")
    assert p == {"verdict": "change", "new_relation": "CAUSES", "reason": ""}
    p = core.sanitize_edge({"verdict": "keep", "new_relation": "CAUSES"}, "GROUNDS_IN")
    assert p["new_relation"] is None
    r = core.sanitize_regen({"subject": "Работаю дома", "confidence": 7}, "Работаю дома")
    assert r["subject"] is None and r["confidence"] == 1.0
    r = core.sanitize_regen({"subject": "Работаю из дома", "confidence": "много"}, "Работаю дома")
    assert r["subject"] == "Работаю из дома" and r["confidence"] is None
    print("ok: ответы ИИ очищаются по белому списку")


# ---- маршруты ----
import routes.memory_viewer as mv
stores = {}
class FakeMem:
    def __init__(self, store):
        self.store = store; self._lock = asyncio.Lock()
    def reload_if_stale(self): return False
    def embed_text(self, text, is_query=False): return None
    def _rebuild_caches_from_gcn(self): pass
    async def _save_async(self): pass
class FakeSvc:
    def __init__(self, m): self.private_memory = m
async def fake_get_service(uid):
    if uid not in stores:
        stores[uid] = FakeSvc(FakeMem(make_store()[0]))
    return stores[uid]
mv.get_memory_service = fake_get_service   # маршруты импортировали имя при загрузке

LLM = {"answer": '{"verdict": "change", "new_relation": "CAUSES", "reason": "причина"}'}
async def fake_call_llm(messages, temp=0.7, max_tokens=0, **_):
    return "Ответ: " + LLM["answer"]
fake_llm.call_llm = fake_call_llm
mv.call_llm = fake_call_llm


def build_app(uid="u1"):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="x" * 40)
    app.include_router(mv.router)
    app.dependency_overrides[require_auth] = lambda: uid
    return app


def test_routes():
    c = TestClient(build_app())
    snap = c.get("/ai/memory/snapshot").json()
    ids = {n["subject"]: n["id"] for n in snap["nodes"]}
    a, cc = ids["Работаю дома"], ids["Есть ноутбук"]
    r = c.get("/ai/memory/impact", params={"id": a})
    assert r.status_code == 200 and r.json()["relied_by"] == ["Есть ноутбук"], r.text
    assert c.get("/ai/memory/impact", params={"id": "../x"}).status_code == 400
    r = c.post("/ai/memory/llm/check-edge", json={"source": cc, "relation": "GROUNDS_IN", "target": a})
    assert r.status_code == 200, r.text
    assert r.json()["proposal"] == {"verdict": "change", "new_relation": "CAUSES", "reason": "причина"}
    r = c.post("/ai/memory/llm/check-edge", json={"source": cc, "relation": "BAD REL", "target": a})
    assert r.status_code == 400
    r = c.post("/ai/memory/llm/regenerate", json={"id": a})
    assert r.status_code == 200 and "proposal" in r.json()
    LLM["answer"] = None
    async def broken(*a, **k): raise ConnectionError("no lm")
    mv.call_llm = broken
    try:
        assert c.post("/ai/memory/llm/check-edge", json={"source": cc, "relation": "GROUNDS_IN", "target": a}).status_code == 503
        assert c.post("/ai/memory/llm/regenerate", json={"id": a}).status_code == 503
    finally:
        mv.call_llm = fake_call_llm
    print("ok: маршруты impact, check-edge, regenerate; ошибки LLM дают 503")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
