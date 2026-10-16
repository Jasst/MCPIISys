"""Тесты правил слияния сообщений (routes/ui_state.py).

Запуск: python3 tests/sync/test_merge_rules.py
Не требует fastapi: нужные функции извлекаются из модуля через ast, а
ввод/вывод на диск заменяется заглушками.
"""
import ast
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "routes" / "ui_state.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
WANT = {"_msg_key", "_merge_msg_fields", "_merge_msgs", "_apply_delta", "commit_final_message"}

FAKE_DISK = {}          # user_id -> state dict (вместо файла)
CHANGES = []

def _read(user_id):
    import copy
    return copy.deepcopy(FAKE_DISK.get(user_id, {"updated": 0, "sessions": [], "messages": {}, "deleted_chats": []}))

def _write_state(user_id, payload):
    import copy
    FAKE_DISK[user_id] = copy.deepcopy(payload)

def _record_change(*a, **k):
    CHANGES.append(a)

def _notify_state(*a, **k):
    pass

NS = {"MAX_MSGS_PER_CHAT": 500, "time": time, "Optional": None, "Dict": dict,
      "List": list, "Any": object, "_read": _read, "_write_state": _write_state,
      "_record_change": _record_change, "_notify_state": _notify_state}
nodes = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in WANT]
assert {n.name for n in nodes} == WANT, {n.name for n in nodes}
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "ui_state", "exec"), NS)

apply_delta = NS["_apply_delta"]
merge_msgs = NS["_merge_msgs"]
commit = NS["commit_final_message"]

FAILED = []

def check(name, cond, detail=""):
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILED.append(name)

def ids(lst):
    return [m["id"] for m in lst]

# 1. Устаревшая позиция в дельте не переставляет сообщения
base = {"messages": {"s": [
    {"id": "u1", "role": "user", "text": "q1"}, {"id": "b1", "role": "assistant", "text": "a1"},
    {"id": "u2", "role": "user", "text": "q2"}, {"id": "b2", "role": "assistant", "text": "partial"}]}}
delta = {"messages": {"s": [{"i": 3, "m": {"id": "b2", "role": "assistant", "text": "full"}},
                            {"i": 3, "m": {"id": "u3", "role": "user", "text": "q3"}}]}}
out, _ = apply_delta(base, delta)
check("stale position keeps order", ids(out["messages"]["s"]) == ["u1", "b1", "u2", "b2", "u3"],
      ids(out["messages"]["s"]))
check("patch applied in place", out["messages"]["s"][3]["text"] == "full")

# 2. Пустой текст из устаревшего черновика не затирает финальный ответ
base = {"messages": {"s": [{"id": "b1", "role": "assistant", "text": "final", "pending": False}]}}
delta = {"messages": {"s": [{"i": 1, "m": {"id": "b1", "role": "assistant", "text": "", "pending": True}}]}}
out, _ = apply_delta(base, delta)
m = out["messages"]["s"][0]
check("empty stale text does not erase answer", m["text"] == "final", m)
check("finished message not reopened as pending", m.get("pending") is False, m)

# 3. Пустой reasoning не затирает известный
base = {"messages": {"s": [{"id": "b1", "role": "assistant", "text": "x", "reasoning": "thoughts"}]}}
delta = {"messages": {"s": [{"i": 1, "m": {"id": "b1", "role": "assistant", "text": "x", "reasoning": ""}}]}}
out, _ = apply_delta(base, delta)
check("empty reasoning keeps known reasoning", out["messages"]["s"][0]["reasoning"] == "thoughts")

# 4. Новое сообщение с ключом дописывается в конец, даже при «кривой» позиции
base = {"messages": {"s": [{"id": "a", "role": "user", "text": "1"}, {"id": "b", "role": "assistant", "text": "2"}]}}
delta = {"messages": {"s": [{"i": 1, "m": {"id": "n", "role": "user", "text": "new"}}]}}
out, _ = apply_delta(base, delta)
check("new keyed message appended at end", ids(out["messages"]["s"]) == ["a", "b", "n"], ids(out["messages"]["s"]))

# 5. Серверная фиксация: вопрос и ответ попадают в правильном порядке
FAKE_DISK.clear(); CHANGES.clear()
FAKE_DISK["u"] = {"updated": 5, "sessions": [{"id": "s"}], "messages": {"s": [
    {"id": "old_u", "role": "user", "text": "old"}, {"id": "old_b", "role": "assistant", "text": "oldA"}]},
    "deleted_chats": []}
ok = commit("u", "s", {"id": "uq", "role": "user", "text": "question"},
            {"id": "bq", "role": "assistant", "text": "answer", "reasoning": "r", "pending": False, "error": None})
check("commit returns True", ok is True)
got = FAKE_DISK["u"]["messages"]["s"]
check("commit appends user then bot in order", ids(got) == ["old_u", "old_b", "uq", "bq"], ids(got))
check("commit keeps answer and reasoning", got[-1]["text"] == "answer" and got[-1]["reasoning"] == "r")
check("commit bumps updated", FAKE_DISK["u"]["updated"] > 5)

# 6. Существующий вопрос пользователя не перезаписывается
ok = commit("u", "s", {"id": "uq", "role": "user", "text": "question (with url)"},
            {"id": "bq", "role": "assistant", "text": "answer2", "pending": False})
got = FAKE_DISK["u"]["messages"]["s"]
check("existing user message text not overwritten", [m for m in got if m["id"] == "uq"][0]["text"] == "question")
check("no duplicate on repeated commit", ids(got).count("bq") == 1, ids(got))

# 7. Устаревший черновик клиента после коммита не стирает ответ сервера
stale = {"messages": {"s": [{"i": 4, "m": {"id": "bq", "role": "assistant", "text": "", "pending": True}}]}}
cur = FAKE_DISK["u"]
out, _ = apply_delta(cur, stale)
bq = [m for m in out["messages"]["s"] if m["id"] == "bq"][0]
check("stale client draft after commit keeps server answer", bq["text"] == "answer2" and bq["pending"] is False, bq)

# 8. Удалённый чат не воскрешается серверной фиксацией
FAKE_DISK["d"] = {"updated": 1, "sessions": [], "messages": {}, "deleted_chats": [{"sid": "gone", "at": 1}]}
check("deleted chat not resurrected", commit("d", "gone", None, {"id": "x", "role": "assistant", "text": "t"}) is False)
check("deleted chat has no messages", "gone" not in FAKE_DISK["d"]["messages"])

# 9. Удаление по id не трогает соседей, даже если позиция в дельте устаревшая
base = {"messages": {"s": [{"id": "a", "role": "user", "text": "1"},
                            {"id": "b", "role": "assistant", "text": "2"},
                            {"id": "c", "role": "user", "text": "3"}]}}
delta = {"messages": {"s": [{"i": 1, "m": None, "id": "c"}]}}
out, _ = apply_delta(base, delta)
check("tombstone by id removes the right message", ids(out["messages"]["s"]) == ["a", "b"],
      ids(out["messages"]["s"]))

# 10. Новое сообщение с ключом и «большой» позицией: без ложного конфликта
base = {"messages": {"s": [{"id": "a", "role": "user", "text": "1"}]}}
delta = {"messages": {"s": [{"i": 5, "m": {"id": "n", "role": "user", "text": "x"}}]}}
out, conf = apply_delta(base, delta)
check("keyed message with gap: appended, no conflict", ids(out["messages"]["s"]) == ["a", "n"] and conf == [],
      (ids(out["messages"]["s"]), conf))

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("ALL PASSED")
