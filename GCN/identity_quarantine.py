"""identity_quarantine.py — карантин кандидатов в идентичность.

Модель не пишет сразу в цепочку. Сначала формулировка попадает в карантин.
Она становится идентичностью, только если:
  - прошла ворота (identity_gate.evaluate) и
  - подтверждена в разные дни (IDENTITY_MIN_DAYS, по умолчанию 2): одноразовая
    мысль, пересказ или шум не попадают в «личность».

Хранение: один JSON-файл на пользователя, запись атомарная.
"""
import difflib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

SIMILAR_RATIO = 0.85
TTL_DAYS = 30


@dataclass
class Candidate:
    id: str
    content: str
    kind: str
    evidence: str
    created: str
    days: List[str] = field(default_factory=list)   # даты (UTC, YYYY-MM-DD), когда формулировку подтвердили
    status: str = "pending"                         # pending | committed | expired | rejected


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Quarantine:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: Dict[str, Candidate] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = {c["id"]: Candidate(**c) for c in raw.get("candidates", [])}
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
            self.items = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"candidates": [asdict(c) for c in self.items.values()]}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def expire(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        n = 0
        for c in self.items.values():
            if c.status == "pending" and datetime.fromisoformat(c.created) < now - timedelta(days=TTL_DAYS):
                c.status = "expired"
                n += 1
        if n:
            self.save()
        return n

    def find_similar(self, content: str) -> Optional[Candidate]:
        best, best_r = None, 0.0
        for c in self.items.values():
            if c.status != "pending":
                continue
            r = difflib.SequenceMatcher(None, content.lower(), c.content.lower()).ratio()
            if r >= SIMILAR_RATIO and r > best_r:
                best, best_r = c, r
        return best

    def propose(self, content: str, kind: str, evidence: str) -> Candidate:
        """Новая формулировка или подтверждение похожей (добавляется день, без дублей)."""
        today = _today()
        existing = self.find_similar(content)
        if existing:
            if today not in existing.days:
                existing.days.append(today)
            self.save()
            return existing
        c = Candidate(id=f"cand_{uuid.uuid4().hex[:10]}", content=content, kind=kind,
                      evidence=evidence, created=datetime.now(timezone.utc).isoformat(), days=[today])
        self.items[c.id] = c
        self.save()
        return c

    def get(self, cand_id: str) -> Optional[Candidate]:
        return self.items.get(cand_id)

    def mark(self, cand_id: str, status: str) -> None:
        if cand_id in self.items:
            self.items[cand_id].status = status
            self.save()
