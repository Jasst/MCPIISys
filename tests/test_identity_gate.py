"""Ворота и карантин идентичности: шум и факты не попадают в личность, запись — после подтверждения
в разные дни, личный слой отделён от общего ядра.
Запуск: python tests/test_identity_gate.py"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import GCN.identity_gate as gate  # noqa: E402
import GCN.identity_quarantine as qmod  # noqa: E402
import GCN.identity_tools as itools  # noqa: E402
from GCN.tool_router import ToolRegistry  # noqa: E402

DIALOGUE = ["Я заметила, что спорю слишком резко, когда чувствую себя не услышанной.",
            "Ответ: инструмент вернул 404"]
GOOD = "Я стараюсь говорить мягче, когда спор задевает меня лично."


def test_gate_accepts_grounded_self_statement():
    r = gate.evaluate(GOOD, "self_observation", "спорю слишком резко", DIALOGUE)
    assert r.ok and r.classification == "identity", r.reasons
    print("1 ok: ворота пропускают самоформулировку с опорой на реплику")


def test_gate_rejects_noise_and_tool_output():
    noisy = "[Результаты инструментов]\nИнструмент: internal__recall\nРезультат: {\"id\": \"fct_abc\"}"
    r = gate.evaluate(noisy, "value", "спорю слишком резко", DIALOGUE)
    assert not r.ok and r.classification == "noise", r.reasons
    code = "```python\nprint('я')\n```"
    assert gate.classify(code) == "noise"
    print("2 ok: служебный текст, JSON, код и id не попадают в идентичность")


def test_gate_redirects_facts_to_memory():
    fact = "Я живу по адресу ул. Ленина, дом 5, телефон +7 999 123-45-67"
    r = gate.evaluate(fact, "value", "спорю слишком резко", DIALOGUE)
    assert not r.ok and r.classification == "memory", r.reasons
    assert "remember" in r.message()
    print("3 ok: факт о пользователе отклоняется с подсказкой использовать remember")


def test_gate_requires_grounding():
    r = gate.evaluate(GOOD, "value", "этой фразы не было в диалоге", DIALOGUE)
    assert not r.ok and any("нет опоры" in x for x in r.reasons), r.reasons
    print("4 ok: без дословной опоры в диалоге формулировка отклоняется")


def test_gate_rejects_duplicate_of_head():
    r = gate.evaluate(GOOD + " ", "self_observation", "спорю слишком резко", DIALOGUE,
                      current_head=GOOD, head_id="id_head")
    assert not r.ok and r.duplicate_of == "id_head"
    print("5 ok: дубликат текущей головы не создаёт новое звено")


def test_quarantine_needs_distinct_days():
    tmp = Path(tempfile.mkdtemp()) / "q.json"
    q = qmod.Quarantine(tmp)
    c1 = q.propose(GOOD, "self_observation", "спорю слишком резко")
    assert c1.days and len(c1.days) == 1
    c2 = q.propose(GOOD, "self_observation", "спорю слишком резко")   # тот же день
    assert c2.id == c1.id and len(c2.days) == 1, "в один день подтверждение не засчитывается дважды"
    c1.days.append("2000-01-01")                                       # имитация другого дня
    q.save()
    q2 = qmod.Quarantine(tmp)                                          # перечитываем с диска
    assert len(q2.get(c1.id).days) == 2
    print("6 ok: карантин сохраняет кандидатов и считает подтверждения по разным дням")


def test_quarantine_expires():
    tmp = Path(tempfile.mkdtemp()) / "q.json"
    q = qmod.Quarantine(tmp)
    c = q.propose(GOOD, "value", "спорю слишком резко")
    c.created = (datetime.now(timezone.utc) - timedelta(days=qmod.TTL_DAYS + 1)).isoformat()
    q.save()
    assert qmod.Quarantine(tmp).expire() == 1
    print("7 ok: непроверенные кандидаты истекают через TTL")


class _Store:
    pass


class _Mem:
    def __init__(self):
        self.private_memory = types_ns(gcn_store=_Store())
        self.shared_memory = types_ns(gcn_store=_Store())


def types_ns(**kw):
    o = _Store()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


class _Controller:
    def __init__(self, history):
        self.memory_service = _Mem()
        self.history = history


def test_tools_commit_only_to_private_layer():
    import GCN.GCN as G
    calls = []

    async def fake_append(service, content, contributor_model, session_id=None,
                          open_question=None, parent_id=None, merge_parent_ids=None, layer="shared"):
        calls.append({"layer": layer, "content": content, "author": contributor_model})
        return {"status": "ok", "id": "snap_1"}

    G_orig_append, G_orig_head = G.append_snapshot, G.get_latest_head
    G.append_snapshot = fake_append
    G.get_latest_head = lambda store: None
    try:
        reg = ToolRegistry()
        ctrl = _Controller([{"role": "user", "content": DIALOGUE[0]}])
        q_path = Path(tempfile.mkdtemp()) / "q.json"
        itools.register_identity_tools(reg, ctrl, q_path)
        propose = reg.get("internal__identity_propose").handler
        commit = reg.get("internal__identity_commit").handler

        async def run():
            p1 = await propose({"content": GOOD, "kind": "self_observation",
                                "evidence": "спорю слишком резко"})
            assert p1["status"] == "pending" and p1["confirmed_days"] == 1
            c = await commit({"candidate_id": p1["candidate_id"]})
            assert c["status"] == "error", "одного дня мало для записи"
            # второй день: та же формулировка предложена в другой день (как в работе, через инструмент)
            orig_today = qmod._today
            qmod._today = lambda: "2000-01-01"
            try:
                p2 = await propose({"content": GOOD, "kind": "self_observation",
                                    "evidence": "спорю слишком резко"})
            finally:
                qmod._today = orig_today
            assert p2["candidate_id"] == p1["candidate_id"] and p2["confirmed_days"] == 2, p2
            c2 = await commit({"candidate_id": p1["candidate_id"]})
            assert c2["status"] == "ok", c2
            # шум через предложение не проходит вовсе
            bad = await propose({"content": "```json {\"id\": \"fct_1\"}```", "kind": "value",
                                 "evidence": "спорю слишком резко"})
            assert bad["status"] == "rejected"

        asyncio.run(run())
        assert len(calls) == 1 and calls[0]["layer"] == "private", calls
        assert calls[0]["author"] == "self"
    finally:
        G.append_snapshot, G.get_latest_head = G_orig_append, G_orig_head
    print("8 ok: identity_commit пишет только в личный слой, после подтверждения, шум отклонён")


if __name__ == "__main__":
    test_gate_accepts_grounded_self_statement()
    test_gate_rejects_noise_and_tool_output()
    test_gate_redirects_facts_to_memory()
    test_gate_requires_grounding()
    test_gate_rejects_duplicate_of_head()
    test_quarantine_needs_distinct_days()
    test_quarantine_expires()
    test_tools_commit_only_to_private_layer()
    print("ALL IDENTITY CHECKS PASSED")
