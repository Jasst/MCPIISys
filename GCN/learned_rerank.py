"""Онлайн-обучение весов гибридного ранжирования по обратной связи человека.

Модель: p(полезно) = sigmoid(alpha * (w · f - bias)), где f — признаки результата
поиска в [0, 1]: semantic, graph, freshness, confidence, evidence.

Обучение: SGD по логистической потере с L2-регуляризацией к априорным весам.
Так редкие отзывы не уводят веса далеко от исходных значений. Метки:
confirm -> 1.0, refute -> 0.0, dispute -> 0.5.

Экспорт: weights() возвращает неотрицательные веса с суммой 1 — ими заменяется
базовый набор весов гибридного поиска.
"""
import json
import math
import os
from typing import Dict, Optional

FEATURES = ("semantic", "graph", "freshness", "confidence", "evidence")
FEEDBACK_LABELS = {"confirm": 1.0, "refute": 0.0, "dispute": 0.5}


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


class RerankLearner:
    def __init__(self, prior: Dict[str, float], lr: float = 0.05, l2: float = 0.01,
                 alpha: float = 6.0, bias: float = 0.5, min_weight: float = 0.02,
                 path: Optional[str] = None):
        missing = [k for k in FEATURES if k not in prior]
        if missing:
            raise ValueError(f"в prior нет признаков: {missing}")
        self.prior = {k: float(prior[k]) for k in FEATURES}
        self.w = dict(self.prior)
        self.lr = lr
        self.l2 = l2
        self.alpha = alpha
        self.bias = bias
        self.min_weight = min_weight
        self.path = path
        self.updates = 0
        if path:
            self.load()

    def _score(self, f: Dict[str, float]) -> float:
        return sum(self.w[k] * float(f.get(k, 0.0)) for k in FEATURES)

    def predict(self, f: Dict[str, float]) -> float:
        return _sigmoid(self.alpha * (self._score(f) - self.bias))

    def update(self, f: Dict[str, float], label: float) -> float:
        """Один шаг SGD. label в [0, 1]. Возвращает предсказание ДО шага."""
        p = self.predict(f)
        for k in FEATURES:
            grad = (p - label) * self.alpha * float(f.get(k, 0.0)) + self.l2 * (self.w[k] - self.prior[k])
            self.w[k] = max(self.min_weight, min(1.0, self.w[k] - self.lr * grad))
        self.updates += 1
        if self.path:
            self.save()
        return p

    def feedback(self, f: Dict[str, float], verdict: str) -> float:
        if verdict not in FEEDBACK_LABELS:
            raise ValueError(f"неизвестный verdict: {verdict}")
        return self.update(f, FEEDBACK_LABELS[verdict])

    def weights(self) -> Dict[str, float]:
        total = sum(self.w.values())
        return {k: v / total for k, v in self.w.items()} if total > 0 else dict(self.prior)

    def save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"w": self.w, "prior": self.prior, "updates": self.updates}, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            w = data.get("w", {})
            if all(k in w for k in FEATURES):
                self.w = {k: float(w[k]) for k in FEATURES}
                self.updates = int(data.get("updates", 0))
        except (OSError, ValueError, TypeError):
            # повреждённый файл не должен ломать память: остаёмся на априорных весах
            self.w = dict(self.prior)
            self.updates = 0
