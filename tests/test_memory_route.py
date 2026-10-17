import asyncio, sys, types
sys.path.insert(0, "/home/claude/proj")

# --- подставки для тяжёлых зависимостей; настоящая логика памяти остаётся ---
fake_mg = types.ModuleType("GCN.memory_graph")
fake_llm = types.ModuleType("GCN.llm_client")
sys.modules["GCN.memory_graph"] = fake_mg
sys.modules["GCN.llm_client"] = fake_llm

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from GCN.GCN import MemoryStore
from dependencies import require_auth

def make_store(tag):
    import random
    random.seed(1)
    st = MemoryStore(embedding_dim=8)
    ids = [st.add_fact(f"{tag}: факт {i}", "claim", "u", confidence=0.4 + i * 0.05,
                       embedding=[random.random() for _ in range(8)]) for i in range(6)]
    st.link(ids[0], ids[1], "RELATED_TO", "t", weight=0.7)
    st.register_contradiction(ids[2], ids[3], "t")
    return st, ids

class FakeMem:
    def __init__(self, store):
        self.store = store
        self._lock = asyncio.Lock()
        self.saved = 0
        self.rebuilt = 0
    def reload_if_stale(self):
        return False
    def _rebuild_caches_from_gcn(self):
        self.rebuilt += 1
    async def _save_async(self):
        self.saved += 1

class FakeService:
    def __init__(self, mem):
        self.private_memory = mem

stores = {}
async def fake_get_service(uid):
    if uid not in stores:
        st, ids = make_store(uid)
        stores[uid] = (FakeService(FakeMem(st)), ids)
    return stores[uid][0]
fake_mg.get_memory_service = fake_get_service

async def fake_call_llm(messages, temp=0.7, max_tokens=0, **_):
    return 'Ответ модели: {"verdict": "wrong", "reason": "не совпадает", "suggested_subject": "исправлено", "suggested_confidence": 2.5, "suggest_retract": false, "evil": "x"}'
fake_llm.call_llm = fake_call_llm

import routes.memory_viewer as mv

def build_app(uid="u1"):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="x" * 40)
    app.include_router(mv.router)
    app.dependency_overrides[require_auth] = lambda: uid
    return app

def test_status_snapshot_detail():
    c = TestClient(build_app("u1"))
    assert c.get("/ai/memory/status").json() == {"enabled": True, "readonly": False}
    snap = c.get("/ai/memory/snapshot").json()
    assert snap["total_nodes"] == 6 and snap["metrics"]["contradictions"] == 1
    assert snap["readonly"] is False and snap["version"]
    oid = snap["nodes"][0]["id"]
    d = c.get("/ai/memory/object", params={"id": oid}).json()
    assert d["id"] == oid and d["subject"].startswith("u1: факт")
    assert c.get("/ai/memory/object", params={"id": "../etc"}).status_code == 400
    assert c.get("/ai/memory/object", params={"id": "fact_missing"}).status_code == 404
    print("1 ok: статус, снимок, детали, проверка id")

def test_user_isolation_and_no_memory_param():
    c = TestClient(build_app("u2"))
    snap = c.get("/ai/memory/snapshot", params={"memory": "u1"}).json()
    subjects = {n["subject"] for n in snap["nodes"]}
    assert subjects and all(s.startswith("u2:") for s in subjects), subjects
    print("2 ok: владелец из сессии, параметр memory игнорируется")

def test_edit_persists_and_saves():
    c = TestClient(build_app("u1"))
    snap = c.get("/ai/memory/snapshot").json()
    oid = snap["nodes"][0]["id"]
    r = c.post("/ai/memory/edit", json={"op": "set_confidence", "args": {"id": oid, "value": 0.9}})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    svc, _ = stores["u1"]
    assert svc.private_memory.store._objects[oid].confidence == 0.9
    assert svc.private_memory.saved == 1 and svc.private_memory.rebuilt == 1
    r = c.post("/ai/memory/edit", json={"op": "drop_all", "args": {}})
    assert r.status_code == 400
    r = c.post("/ai/memory/edit", json={"op": "set_confidence", "args": {"id": oid, "value": "много"}})
    assert r.status_code == 400
    r = c.post("/ai/memory/edit", json={"op": "set_subject", "args": {"id": "../x", "text": "a"}})
    assert r.status_code == 400
    assert svc.private_memory.saved == 1  # неудачные операции ничего не сохраняют
    print("3 ok: правка сохраняется через сервис, ошибки не пишут на диск")

def test_readonly_and_disabled(monkeypatch=None):
    c = TestClient(build_app("u1"))
    snap = c.get("/ai/memory/snapshot").json()
    oid = snap["nodes"][0]["id"]
    mv.READONLY = True
    try:
        assert c.post("/ai/memory/edit", json={"op": "retract", "args": {"id": oid}}).status_code == 403
        assert c.get("/ai/memory/snapshot").json()["readonly"] is True
    finally:
        mv.READONLY = False
    mv.ENABLED = False
    try:
        assert c.get("/ai/memory/status").status_code == 404
        assert c.get("/ai/memory/page").status_code == 404
    finally:
        mv.ENABLED = True
    print("4 ok: режим только чтение (403) и выключение (404)")

def test_llm_proposal_sanitized_and_unavailable():
    c = TestClient(build_app("u1"))
    oid = c.get("/ai/memory/snapshot").json()["nodes"][0]["id"]
    r = c.post("/ai/memory/llm/review", json={"id": oid})
    assert r.status_code == 200, r.text
    p = r.json()["proposal"]
    assert p["verdict"] == "wrong" and p["suggested_subject"] == "исправлено"
    assert p["suggested_confidence"] == 1.0          # 2.5 ограничено диапазоном
    assert "evil" not in p                            # лишние поля отброшены
    # LLM не применяет правку сама: доверие записи не изменилось
    svc, _ = stores["u1"]
    assert svc.private_memory.store._objects[oid].confidence != 1.0

    async def broken(*a, **k):
        raise ConnectionError("no lm studio")
    mv.call_llm = broken
    try:
        assert c.post("/ai/memory/llm/review", json={"id": oid}).status_code == 503
    finally:
        mv.call_llm = fake_call_llm
    print("5 ok: предложение очищено, LLM не меняет память, недоступность LLM даёт 503")

def test_page_served_and_auth_required():
    c = TestClient(build_app("u1"))
    r = c.get("/ai/memory/page")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "Моя память" in r.text and "/ai/memory" in r.text
    assert r.headers.get("cache-control") == "no-store"
    anon = TestClient(FastAPI())  # без подмены require_auth: сессии нет
    anon.app.add_middleware(SessionMiddleware, secret_key="x" * 40)
    anon.app.include_router(mv.router)
    assert anon.get("/ai/memory/snapshot").status_code == 401
    assert anon.post("/ai/memory/edit", json={"op": "retract", "args": {}}).status_code == 401
    print("6 ok: страница отдаётся, без входа — 401")

if __name__ == "__main__":
    for t in [test_status_snapshot_detail, test_user_isolation_and_no_memory_param, test_edit_persists_and_saves,
              test_readonly_and_disabled, test_llm_proposal_sanitized_and_unavailable, test_page_served_and_auth_required]:
        t()
    print("ALL PASSED")
