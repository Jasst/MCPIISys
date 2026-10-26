"""Пакетные проверки Авто-ИИ: один запрос вместо многих, и «без ИИ» по умолчанию."""
import json
import test_memory_review as tr  # общие заглушки памяти и LLM

mv, LLM, fake_llm = tr.mv, tr.LLM, tr.fake_llm


def _client(uid="u7"):
    from fastapi.testclient import TestClient
    return TestClient(tr.build_app(uid))


def test_auto_scan_without_llm_makes_no_model_call():
    calls = []
    orig = mv.call_llm

    async def spy(messages, **kw):
        calls.append(1)
        return "{}"

    mv.call_llm = spy
    try:
        r = _client().post("/ai/memory/llm/auto-scan", json={"limit": 10})  # use_llm по умолчанию False
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["llm"] is False and body["proposals"] == []
        assert "dups" in body and "link_suggestions" in body
        assert calls == [], "без use_llm модель вызываться не должна"
    finally:
        mv.call_llm = orig


def test_auto_scan_with_llm_calls_model_once():
    calls = []
    orig = mv.call_llm

    async def spy(messages, **kw):
        calls.append(1)
        return '{"actions": []}'

    mv.call_llm = spy
    try:
        r = _client().post("/ai/memory/llm/auto-scan", json={"limit": 10, "use_llm": True})
        assert r.status_code == 200, r.text
        body = r.json()
        # модель вызывается только если есть подозрительные кандидаты, и не больше одного раза
        assert body["llm"] == (body["candidates"] > 0)
        assert len(calls) == (1 if body["candidates"] > 0 else 0)
    finally:
        mv.call_llm = orig


def test_check_edges_batch_is_one_call_and_maps_by_index():
    snap = _client().get("/ai/memory/snapshot").json()
    ids = {n["subject"]: n["id"] for n in snap["nodes"]}
    a, c = ids["Работаю дома"], ids["Есть ноутбук"]
    calls = []
    orig = mv.call_llm

    async def spy(messages, **kw):
        calls.append(1)
        return ('Ответ: {"edges": [{"index": 1, "verdict": "keep", "reason": "ок"},'
                ' {"index": 0, "verdict": "change", "new_relation": "CAUSES", "reason": "причина"}]}')

    mv.call_llm = spy
    try:
        items = [{"source": c, "relation": "GROUNDS_IN", "target": a},
                 {"source": a, "relation": "RELATED_TO", "target": c}]
        r = _client().post("/ai/memory/llm/check-edges", json={"items": items})
        assert r.status_code == 200, r.text
        props = r.json()["proposals"]
        assert len(calls) == 1, "все связи — один запрос к модели"
        assert len(props) == 2
        assert props[0]["verdict"] == "change" and props[0]["new_relation"] == "CAUSES"
        assert props[1]["verdict"] == "keep"
    finally:
        mv.call_llm = orig


def test_check_edges_rejects_bad_relation_and_empty_batch():
    c = _client()
    assert c.post("/ai/memory/llm/check-edges", json={"items": []}).status_code == 422
    bad = {"items": [{"source": "a", "relation": "bad rel", "target": "b"}]}
    assert c.post("/ai/memory/llm/check-edges", json=bad).status_code in (400, 404)


# ---------- новые связи с помощью ИИ ----------
import types
import numpy as np
from GCN import memory_viewer_core as core


class _G:
    def __init__(self):
        self._edges = {}

    def add(self, a, r, t):
        self._edges.setdefault(a, []).append((r, t))


def _toy_store():
    st = types.SimpleNamespace(_objects={}, _embedding_index={}, _graph=_G())
    rng = np.random.default_rng(3)
    base = rng.normal(size=16)
    for i in range(12):
        oid = f"m{i:02d}"
        st._objects[oid] = types.SimpleNamespace(subject=f"факт {i}")
        v = base + rng.normal(scale=0.05, size=16) if i < 6 else rng.normal(size=16)
        st._embedding_index[oid] = list(map(float, v))
    st._graph.add("m00", "RELATED_TO", "m01")  # m01 уже связана с m00
    return st


def test_link_candidates_skip_self_and_linked():
    st = _toy_store()
    cands = core.link_candidates(st, "m00", k=10)
    ids = [c["id"] for c in cands]
    assert "m00" not in ids and "m01" not in ids
    assert len(ids) <= 10
    sims = [c["similarity"] for c in cands]
    assert sims == sorted(sims, reverse=True)


def test_link_suggestions_whitelist_and_direction():
    cands = [{"id": "m02", "subject": "A"}, {"id": "m03", "subject": "B"}]
    raw = {"links": [
        {"id": "m02", "direction": "out", "relation": "CAUSES", "reason": "x"},
        {"id": "m03", "direction": "in", "relation": "grounds_in", "reason": "y"},  # регистр нормализуется
        {"id": "m03", "direction": "in", "relation": "CONTRADICTS", "reason": "нельзя"},
        {"id": "ghost", "direction": "out", "relation": "CAUSES"},                  # чужой id
        {"id": "m02", "direction": "sideways", "relation": "CAUSES"},               # плохое направление
        {"id": "m00", "direction": "out", "relation": "CAUSES"},                    # сама запись
    ]}
    out = core.sanitize_link_suggestions(raw, "m00", cands)
    assert [(o["source"], o["target"], o["relation"]) for o in out] == [
        ("m00", "m02", "CAUSES"), ("m03", "m00", "GROUNDS_IN")]


def test_suggest_links_route_uses_one_call_only_with_candidates():
    calls = []
    orig = mv.call_llm

    async def spy(messages, **kw):
        calls.append(1)
        return '{"links": []}'

    mv.call_llm = spy
    try:
        c = _client("u8")
        r = c.post("/ai/memory/llm/suggest-links", json={"id": "zz-not-real"})
        assert r.status_code in (400, 404), r.text
        snap = c.get("/ai/memory/snapshot").json()
        nid = snap["nodes"][0]["id"]
        r = c.post("/ai/memory/llm/suggest-links", json={"id": nid, "k": 5})
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(calls) == (1 if body["candidates"] else 0)
        assert body["llm"] == bool(body["candidates"])
    finally:
        mv.call_llm = orig
