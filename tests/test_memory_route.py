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
    def embed_text(self, text, is_query=False):
        return [float(len(text)) / 100.0] + [0.0] * 7
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

def _apply_delta(prev, d):
    from GCN.memory_viewer_core import edge_key
    nodes = {n["id"]: n for n in prev["nodes"]}
    for k in d["nodes_removed"]:
        nodes.pop(k)
    for n in d["nodes_added"] + d["nodes_changed"]:
        nodes[n["id"]] = n
    edges = {edge_key(e): e for e in prev["edges"]}
    for k in d["edges_removed"]:
        edges.pop(k)
    for e in d["edges_added"]:
        edges[edge_key(e)] = e
    return nodes, edges

def test_snapshot_unchanged_and_delta():
    c = TestClient(build_app("u3"))
    full = c.get("/ai/memory/snapshot").json()
    v1 = full["version"]
    assert c.get("/ai/memory/snapshot", params={"since": v1}).json() == {"version": v1, "unchanged": True}
    oid = full["nodes"][0]["id"]
    assert c.post("/ai/memory/edit", json={"op": "set_confidence", "args": {"id": oid, "value": 0.93}}).status_code == 200
    d = c.get("/ai/memory/snapshot", params={"since": v1}).json()
    assert d["delta"] is True and d["base"] == v1 and d["version"] != v1
    assert [n["id"] for n in d["nodes_changed"]] == [oid]
    assert d["nodes_added"] == [] and d["nodes_removed"] == []
    nodes, edges = _apply_delta(full, d)
    fresh = c.get("/ai/memory/snapshot").json()
    assert nodes == {n["id"]: n for n in fresh["nodes"]}
    assert edges == {f"{e['s']}|{e['r']}|{e['t']}": e for e in fresh["edges"]}
    other = c.get("/ai/memory/snapshot", params={"since": "bogus"}).json()
    assert other.get("delta") is None and len(other["nodes"]) == fresh["total_nodes"]
    print("7 ok: unchanged, дельта совпадает с полным снимком, рассинхрон -> полный снимок")

def test_snapshot_history_serves_delta_to_second_tab_and_pca_on_demand():
    c = TestClient(build_app("u4"))
    v1 = c.get("/ai/memory/snapshot").json()
    assert "pca" in v1 and v1["pca"] == {}, "PCA по умолчанию не считается"
    oid = v1["nodes"][0]["id"]
    assert c.post("/ai/memory/edit", json={"op": "set_confidence", "args": {"id": oid, "value": 0.2}}).status_code == 200
    v2 = c.get("/ai/memory/snapshot").json()           # вторая «вкладка» уже видит v2
    assert v2["version"] != v1["version"]
    # первая вкладка всё ещё на v1: из истории берётся база, отдаётся дельта, а не полный снимок
    d = c.get("/ai/memory/snapshot", params={"since": v1["version"]}).json()
    assert d.get("delta") is True and d["base"] == v1["version"]
    nodes, _ = _apply_delta(v1, d)
    assert nodes == {n["id"]: n for n in v2["nodes"]}
    # pca=1 отдаёт проекцию
    full_pca = c.get("/ai/memory/snapshot", params={"pca": 1}).json()
    assert full_pca["pca"] and all(len(v) == 2 for v in full_pca["pca"].values())
    print("8 ok: история снимков для второй вкладки, PCA только по запросу")

def test_snapshot_survives_legacy_and_broken_records():
    from datetime import datetime
    st = make_store("legacy")[0]
    ids = list(st._objects.keys())
    st._objects[ids[0]].created = datetime(2025, 1, 1)      # naive-дата из старых записей
    st._objects[ids[1]].confidence = None                    # доверие не задано
    st._embedding_index[ids[2]] = [0.1, 0.2]                 # эмбеддинг другой размерности
    from GCN import memory_viewer_core as core
    snap = core.build_snapshot(st, max_nodes=800)
    assert snap["total_nodes"] == 6 and all(n["created"].endswith(("+00:00", "Z")) or "+" in n["created"] for n in snap["nodes"])
    assert snap["metrics"]["oldest"] is not None
    print("9 ok: старые данные (naive-даты, пустое доверие, кривые эмбеддинги) не ломают снимок")

def test_edit_text_and_retract_end_to_end():
    c = TestClient(build_app("u6"))
    snap = c.get("/ai/memory/snapshot").json()
    svc, _ = stores["u6"]
    st = svc.private_memory.store
    a = snap["nodes"][0]["id"]
    old_vec = st.get_embedding(a)
    r = c.post("/ai/memory/edit", json={"op": "set_subject", "args": {"id": a, "text": "новый текст записи"}})
    assert r.status_code == 200, r.text
    assert st._objects[a].subject == "новый текст записи"
    assert st.get_embedding(a) != old_vec, "эмбеддинг не пересчитан после смены текста"
    assert st.get_embedding(a) == svc.private_memory.embed_text("новый текст записи")
    assert c.post("/ai/memory/edit", json={"op": "set_subject", "args": {"id": a, "text": "   "}}).status_code == 400
    d = c.get("/ai/memory/snapshot", params={"since": snap["version"]}).json()
    assert [n["subject"] for n in d["nodes_changed"] if n["id"] == a] == ["новый текст записи"]
    r = c.post("/ai/memory/edit", json={"op": "retract", "args": {"id": a, "reason": "тест"}})
    assert r.status_code == 200 and a not in st._objects
    assert c.get("/ai/memory/object", params={"id": a}).status_code == 404
    assert c.post("/ai/memory/edit", json={"op": "retract", "args": {"id": a}}).status_code == 400
    d2 = c.get("/ai/memory/snapshot", params={"since": d["version"]}).json()
    assert a in d2["nodes_removed"]
    print("10 ok: смена текста пересчитывает эмбеддинг, удаление убирает узел, повтор даёт 400")

if __name__ == "__main__":
    for t in [test_status_snapshot_detail, test_user_isolation_and_no_memory_param, test_edit_persists_and_saves,
              test_readonly_and_disabled, test_llm_proposal_sanitized_and_unavailable, test_page_served_and_auth_required, test_snapshot_unchanged_and_delta, test_snapshot_history_serves_delta_to_second_tab_and_pca_on_demand, test_snapshot_survives_legacy_and_broken_records, test_edit_text_and_retract_end_to_end]:
        t()
    print("ALL PASSED")