"""NLI-классификация пар утверждений (противоречие / следствие / нейтрально).

Используется до LLM-верификатора противоречий: если NLI уверенно говорит, что
утверждения не противоречат, пара сразу помечается как ложное противоречие, и LLM
не вызывается. Работает только при заданной модели (NLI_MODEL). Без модели
decide() возвращает None, и поведение остаётся прежним (LLM).

Порядок меток зависит от модели: для cross-encoder/nli-* обычно
contradiction, entailment, neutral. Задаётся через NLI_LABELS.
"""
import logging
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def softmax(logits: Sequence[float]) -> List[float]:
    m = max(logits)
    exps = [math.exp(x - m) for x in logits]
    total = sum(exps)
    return [e / total for e in exps]


class NLIJudge:
    def __init__(self, labels: Sequence[str] = ("contradiction", "entailment", "neutral"),
                 scorer: Optional[Callable[[str, str], Sequence[float]]] = None,
                 model_name: str = ""):
        self.labels = tuple(labels)
        self._scorer = scorer
        if scorer is None and model_name:
            try:
                from sentence_transformers import CrossEncoder
                model = CrossEncoder(model_name)
                self._scorer = lambda a, b: model.predict([(a, b)])[0]
            except Exception as e:  # модель не установлена или не скачана — работаем без NLI
                logger.warning(f"NLI: модель {model_name!r} недоступна ({e}); используется LLM")
                self._scorer = None

    @property
    def available(self) -> bool:
        return self._scorer is not None

    def probabilities(self, a: str, b: str) -> Dict[str, float]:
        logits = list(self._scorer(a, b))
        if len(logits) != len(self.labels):
            raise ValueError(f"модель вернула {len(logits)} значений, ожидалось {len(self.labels)}")
        return dict(zip(self.labels, softmax(logits)))

    def decide(self, a: str, b: str, threshold: float) -> Optional[str]:
        """Метка, если её вероятность >= threshold, иначе None (решает LLM)."""
        if not self.available:
            return None
        probs = self.probabilities(a, b)
        label, p = max(probs.items(), key=lambda kv: kv[1])
        return label if p >= threshold else None
