"""identity_tools.py — как модель управляет своей идентичностью.

Два шага, каждый — осознанный выбор модели:
  identity_propose(content, kind, evidence)  — предложить формулировку о себе; она уходит в карантин.
  identity_commit(candidate_id)              — записать её в ЛИЧНУЮ цепочку ТЕКУЩЕЕ_Я, если она
                                               прошла ворота и подтверждена в разные дни.

Модель не пишет в общее курируемое ядро и не может обойти ворота. Факты о мире и пользователе
сюда не попадают: для них есть remember.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List

from . import identity_gate as gate
from .identity_quarantine import Quarantine
from .tool_router import ToolRegistry

logger = logging.getLogger(__name__)

try:
    from . import config_ai as _cfg
    IDENTITY_MIN_DAYS = int(getattr(_cfg, "IDENTITY_MIN_DAYS", 2))
except Exception:  # noqa: BLE001
    IDENTITY_MIN_DAYS = 2

DEFAULT_QUARANTINE = Path(__file__).resolve().parent.parent / "ai_memory_v3" / "identity_quarantine.json"
CONTRIBUTOR = "self"   # автор звеньев, которые модель записала о себе


def _dialogue(controller, limit: int = 12) -> List[str]:
    hist = getattr(controller, "history", None) or []
    return [str(m.get("content", "")) for m in hist[-limit:] if isinstance(m, dict)]


def _current_head(controller):
    """Текущая голова ЛИЧНОЙ цепочки (без подмены общим ядром)."""
    from GCN.GCN import get_latest_head
    store = controller.memory_service.private_memory.gcn_store
    head = get_latest_head(store)
    if head is None:
        return None, None
    meta = head.object if isinstance(head.object, dict) else {}
    return (meta.get("content") or ""), head.id


def register_identity_tools(registry: ToolRegistry, controller, quarantine_path: Path = DEFAULT_QUARANTINE) -> Quarantine:
    q = Quarantine(quarantine_path)

    async def _propose(args: Dict[str, Any]) -> Dict[str, Any]:
        content = (args.get("content") or "").strip()
        kind = (args.get("kind") or "").strip()
        evidence = (args.get("evidence") or "").strip()
        head_text, head_id = _current_head(controller)
        res = gate.evaluate(content, kind, evidence, _dialogue(controller), head_text, head_id)
        if not res.ok:
            return {"status": "rejected", "classification": res.classification,
                    "message": res.message(), "duplicate_of": res.duplicate_of}
        cand = q.propose(content, kind, evidence)
        ready = len(cand.days) >= IDENTITY_MIN_DAYS
        return {"status": "pending", "candidate_id": cand.id, "confirmed_days": len(cand.days),
                "needed_days": IDENTITY_MIN_DAYS,
                "message": ("Готово к записи: вызови identity_commit с этим candidate_id." if ready else
                            f"Сохранено в карантин. Запись в идентичность — после подтверждения в "
                            f"{IDENTITY_MIN_DAYS} разных днях (сейчас {len(cand.days)}).")}

    async def _commit(args: Dict[str, Any]) -> Dict[str, Any]:
        from GCN.GCN import append_snapshot
        cand = q.get((args.get("candidate_id") or "").strip())
        if cand is None or cand.status != "pending":
            return {"status": "error", "message": "кандидат не найден или уже обработан"}
        if len(cand.days) < IDENTITY_MIN_DAYS:
            return {"status": "error", "message": f"нужно подтверждение в {IDENTITY_MIN_DAYS} разных днях "
                                                  f"(сейчас {len(cand.days)})"}
        head_text, head_id = _current_head(controller)
        res = gate.evaluate(cand.content, cand.kind, cand.evidence, _dialogue(controller), head_text, head_id)
        if not res.ok:
            q.mark(cand.id, "rejected")
            return {"status": "rejected", "message": res.message()}
        out = await append_snapshot(
            controller.memory_service, content=cand.content, contributor_model=CONTRIBUTOR,
            open_question=None, layer="private")
        if out.get("status") != "ok":
            return {"status": "error", "message": out.get("message", "не записано")}
        q.mark(cand.id, "committed")
        return {"status": "ok", "identity_id": out.get("id"), "message": "записано в личную цепочку ТЕКУЩЕЕ_Я"}

    registry.register(
        name="identity_propose",
        description=("Предлагает формулировку о СЕБЕ (ценность, стиль, наблюдение о себе), опирающуюся на "
                     "реплику диалога. НЕ для фактов о пользователе или мире (для них remember). "
                     "Аргументы: content (str, 30–600 симв., от первого лица), kind (value|style|"
                     "self_observation|commitment|open_question), evidence (дословная цитата из диалога)."),
        parameters={"type": "object", "properties": {
            "content": {"type": "string"},
            "kind": {"type": "string", "enum": list(gate.KINDS)},
            "evidence": {"type": "string"}},
            "required": ["content", "kind", "evidence"]},
        handler=_propose, server="internal", timeout_seconds=20)

    registry.register(
        name="identity_commit",
        description=("Записывает подтверждённого кандидата (candidate_id из identity_propose) в личную цепочку "
                     "ТЕКУЩЕЕ_Я. Работает только после подтверждения в разные дни. Аргумент: candidate_id."),
        parameters={"type": "object", "properties": {"candidate_id": {"type": "string"}},
                    "required": ["candidate_id"]},
        handler=_commit, server="internal", timeout_seconds=30)
    return q
