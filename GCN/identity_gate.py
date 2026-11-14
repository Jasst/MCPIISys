"""identity_gate.py — ворота записи в цепочку ТЕКУЩЕЕ_Я.

Принцип: идентичность — это то, что модель САМА вывела из диалога о себе.
Всё остальное — память (факты о мире и пользователе) или мусор.

Ворота решают три вопроса:
  1. Это вообще идентичность, а не факт и не результат инструмента?  (classify)
  2. Опирается ли формулировка на реплику из диалога (evidence)?      (evidence_grounded)
  3. Не дубликат ли и достаточно ли качественна формулировка?          (evaluate)

Функции чистые (без I/O и LLM) — их можно тестировать отдельно.
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import List, Optional, Sequence

KINDS = {
    "value": "что для меня важно",
    "style": "как я склонна/склонен общаться и думать",
    "self_observation": "что я заметила о себе в диалогах",
    "commitment": "что я решила делать иначе",
    "open_question": "вопрос, к которому я хочу вернуться",
}
MIN_LEN, MAX_LEN = 30, 600
DUPLICATE_RATIO = 0.90

# Маркеры технического шума: результаты инструментов, служебные блоки, код, JSON, ссылки.
_NOISE_PATTERNS = [
    r"```", r"===", r"\[Повтор", r"\[Из прошлых ходов\]", r"\[Результаты",
    r"Инструмент:", r"Аргументы:", r"Результат:", r"https?://", r"^\s*[\{\[]",
    r"\bfct_[0-9a-z]+", r"\bkeyerror\b", r"traceback", r"\bNone\b",
]
# Признаки фактов о мире/пользователе: даты, телефоны, адреса, числа с единицами, id.
_FACT_PATTERNS = [
    r"\b\d{4}-\d{2}-\d{2}\b", r"\+?\d[\d\s\-()]{8,}\d", r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b",
    r"\b(?:ул\.|улица|проспект|пр-т|дом\s+\d)", r"\b\d+\s*(?:руб|usd|eur|€|\$|км|кг|см)\b",
]
# Маркеры «о себе» — подсказка, что формулировка идентичностная.
_SELF_MARKERS = re.compile(
    r"\b(я|меня|мне|мой|моя|мое|моё|мои|себя|себе|во мне|я сама|я сам)\b", re.IGNORECASE)


@dataclass
class GateResult:
    ok: bool
    classification: str                       # 'identity' | 'memory' | 'noise'
    reasons: List[str] = field(default_factory=list)
    duplicate_of: Optional[str] = None        # id текущей головы, если формулировка совпадает с ней

    def message(self) -> str:
        if self.ok:
            return "принято"
        hint = {
            "memory": "Это похоже на факт о мире или пользователе — используй remember, не идентичность.",
            "noise": "Это похоже на технический шум или служебный текст — в идентичность не попадает.",
        }.get(self.classification, "Формулировка не прошла проверку.")
        return f"Отклонено: {'; '.join(self.reasons)}. {hint}"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def is_grounded(evidence: str, dialogue: Sequence[str], min_len: int = 12) -> bool:
    """Опора на диалог: evidence (цитата) должна дословно присутствовать в одной из реплик."""
    ev = _norm(evidence)
    if len(ev) < min_len:
        return False
    return any(ev in _norm(line) for line in dialogue if line)


def classify(content: str) -> str:
    """Грубая классификация: identity | memory | noise. Не заменяет проверку, а направляет модель."""
    text = content or ""
    if any(re.search(p, text, re.IGNORECASE | re.MULTILINE) for p in _NOISE_PATTERNS):
        return "noise"
    if any(re.search(p, text, re.IGNORECASE) for p in _FACT_PATTERNS):
        return "memory"
    if not _SELF_MARKERS.search(text):
        return "memory"         # нет «я» — это утверждение о мире, а не о себе
    return "identity"


def evaluate(content: str, kind: str, evidence: str, dialogue: Sequence[str],
             current_head: Optional[str] = None, head_id: Optional[str] = None) -> GateResult:
    reasons: List[str] = []
    text = (content or "").strip()

    if kind not in KINDS:
        reasons.append(f"неизвестный kind; допустимо: {', '.join(KINDS)}")
    if not (MIN_LEN <= len(text) <= MAX_LEN):
        reasons.append(f"длина {len(text)} вне диапазона {MIN_LEN}–{MAX_LEN} символов")

    cls = classify(text)
    if cls == "noise":
        reasons.append("технический шум (служебные маркеры, код, ссылки, JSON или id)")
    elif cls == "memory":
        reasons.append("факт о мире или пользователе, а не о себе")

    if not is_grounded(evidence, dialogue):
        reasons.append("нет опоры: цитата из диалога (evidence) не найдена в реплике дословно")

    if current_head and head_id:
        ratio = SequenceMatcher(None, _norm(text), _norm(current_head)).ratio()
        if ratio >= DUPLICATE_RATIO:
            reasons.append(f"дубликат текущей головы {head_id} (сходство {ratio:.0%})")
            return GateResult(ok=False, classification=cls, reasons=reasons, duplicate_of=head_id)

    return GateResult(ok=not reasons, classification=cls, reasons=reasons)
