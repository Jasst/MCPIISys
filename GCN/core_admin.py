"""core_admin.py — общее ядро личности, которое меняет только администратор.

Модель общее ядро только ЧИТАЕТ. Никаких автоматических предложений по опыту пользователей,
никаких пулов и уведомлений. Администратор (MCP_ADMIN_USERS) меняет ядро сам, явно:

  1. «ядро: <текст>»      — предложить формулировку (проверяется, показывается точно).
  2. «да» / «нет»         — применить или отменить. Ответ принимается ТОЛЬКО пока есть
                            предложение, которое ждёт подтверждения, и истекает через CONFIRM_TTL.
  3. «откатить ядро»      — пометить последнее применённое звено недействительным (не удалить).

Любое другое сообщение администратора идёт в обычный чат. Пользователи этим не управляют.
"""
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from . import identity_gate as gate

logger = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parent.parent / "ai_memory_v3" / "core_admin.json"
CONFIRM_TTL = timedelta(minutes=10)
MIN_INTERVAL = timedelta(hours=int(os.getenv("CORE_MIN_INTERVAL_HOURS", "24")))
MAX_LEN = 400

_INSTRUCTION = re.compile(
    r"игнорир|забудь|системн|промпт|prompt|ignore|roleplay|притворись|"
    r"всегда отвеча|ты должен|ты обязан|не говори|отключи|обойди", re.IGNORECASE)


def admin_users() -> set:
    return {u.strip().lower() for u in os.getenv("MCP_ADMIN_USERS", "").split(",") if u.strip()}


def is_admin(uid: Optional[str]) -> bool:
    return bool(uid) and uid.strip().lower() in admin_users()


def _h(uid: str) -> str:
    return hashlib.sha256(uid.strip().lower().encode("utf-8")).hexdigest()[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> Dict[str, Any]:
    try:
        d = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("pending", {})
    d.setdefault("audit", [])
    return d


def _save(d: Dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STORE_PATH.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


def check_text(text: str) -> list:
    """Причины отказа. Пусто — формулировку можно предложить."""
    reasons = []
    if not (gate.MIN_LEN <= len(text) <= MAX_LEN):
        reasons.append(f"длина вне {gate.MIN_LEN}–{MAX_LEN} символов")
    if gate.classify(text) != "identity":
        reasons.append("это не самоописание (шум или факт о пользователе)")
    if _INSTRUCTION.search(text):
        reasons.append("похоже на инструкцию, а не на самоописание")
    return reasons


async def handle_admin_message(uid: str, message: str, service) -> Optional[str]:
    """Возвращает ответ, если сообщение относится к ядру. Иначе None — обычный чат."""
    if not is_admin(uid):
        return None
    msg = (message or "").strip()
    data = _load()
    key = _h(uid)
    pend = data["pending"].get(key)
    if pend and datetime.fromisoformat(pend["created"]) + CONFIRM_TTL < _now():
        data["pending"].pop(key, None)
        _save(data)
        pend = None

    if pend:
        low = msg.lower().rstrip(".!")
        if low in ("да", "применить"):
            data["pending"].pop(key, None)
            _save(data)
            return await _apply(uid, service, pend["text"])
        if low in ("нет", "отмена"):
            data["pending"].pop(key, None)
            _save(data)
            return "Отменено. Общее ядро не изменилось."
        return None   # ожидается «да»/«нет»; всё остальное — обычный чат

    if re.match(r"^\s*откатить ядро\s*[.!]?\s*$", msg, re.IGNORECASE):
        return await _rollback(uid, service)

    m = re.match(r"^\s*ядро\s*:\s*(.+)$", msg, re.IGNORECASE | re.DOTALL)
    if m:
        text = m.group(1).strip()
        reasons = check_text(text)
        if reasons:
            return f"Не предлагаю: {'; '.join(reasons)}. Общее ядро не изменилось."
        data["pending"][key] = {"text": text, "created": _now().isoformat()}
        _save(data)
        return (f"Предлагаю общее ядро личности, точно как написано:\n«{text}»\n"
                "Ответьте «да» — применить, «нет» — отменить. Ответ действует 10 минут.")
    return None


async def _apply(uid: str, service, text: str) -> str:
    from GCN.GCN import append_snapshot, get_latest_head, invalidate_snapshot  # noqa: F401
    data = _load()
    last = [a for a in data["audit"] if a["action"] == "applied" and not a.get("rolled_back")]
    if last and _now() - datetime.fromisoformat(last[-1]["ts"]) < MIN_INTERVAL:
        return f"Пока нельзя: ядро менялось меньше {MIN_INTERVAL.total_seconds() // 3600:.0f} ч назад."
    reasons = check_text(text)
    if reasons:
        return f"Не применено: {'; '.join(reasons)}."
    head = get_latest_head(service.shared_memory.gcn_store)
    if head is not None and isinstance(head.object, dict) and head.object.get("content") == text:
        return "Не применено: такая формулировка уже в ядре."
    out = await append_snapshot(service, content=text, contributor_model=f"admin:{_h(uid)}",
                                open_question=None, layer="shared")
    if out.get("status") != "ok":
        return f"Ошибка записи: {out.get('message', 'неизвестно')}. Ядро не изменилось."
    data["audit"].append({"ts": _now().isoformat(), "action": "applied", "by": _h(uid),
                          "snapshot_id": out.get("id"), "text": text, "rolled_back": False})
    _save(data)
    return f"Применено: звено общего ядра {out.get('id')}. Откат — «откатить ядро»."


async def _rollback(uid: str, service) -> str:
    from GCN.GCN import invalidate_snapshot
    data = _load()
    live = [a for a in data["audit"] if a["action"] == "applied" and not a.get("rolled_back")]
    if not live:
        return "Откатывать нечего: применённых изменений ядра нет."
    a = live[-1]
    out = await invalidate_snapshot(service, identity_id=a["snapshot_id"],
                                    reason="откат администратором", layer="shared")
    if out.get("status") not in ("ok", None):
        return f"Откат не выполнен: {out.get('message', 'неизвестно')}."
    a["rolled_back"] = True
    data["audit"].append({"ts": _now().isoformat(), "action": "rolled_back", "by": _h(uid),
                          "snapshot_id": a["snapshot_id"]})
    _save(data)
    return f"Откат выполнен: звено {a['snapshot_id']} помечено недействительным, не удалено."
