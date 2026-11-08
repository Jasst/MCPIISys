"""Предиктор следующего концепта по эмбеддингам (двухслойная сеть на numpy).

Вход: эмбеддинг текущего состояния (реплика/факт). Выход: вектор, к которому
направляется предсказание; кандидаты ранжируются косинусом.
Обучение: полный батч, градиент косинусной потери 1 - cos(pred, target)
(градиент выписан вручную, torch не нужен).

Модель НЕ применяется сразу. fit_with_gate() сравнивает hit@k на отложенной выборке
с базовым уровнем «популярность целевых концептов» и принимает модель только при
выигрыше больше margin. Иначе predict() не используется.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


def _normalize_rows(M: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(M, axis=-1, keepdims=True)
    return M / np.where(n == 0, 1.0, n)


@dataclass
class TransitionModel:
    dim: int
    hidden: int = 64
    seed: int = 0
    W1: Optional[np.ndarray] = None
    b1: Optional[np.ndarray] = None
    W2: Optional[np.ndarray] = None
    b2: Optional[np.ndarray] = None
    report: Dict = field(default_factory=dict)

    def __post_init__(self):
        if self.W1 is None:
            rng = np.random.default_rng(self.seed)
            self.W1 = rng.normal(0, 1 / math.sqrt(self.dim), (self.dim, self.hidden))
            self.b1 = np.zeros(self.hidden)
            self.W2 = rng.normal(0, 1 / math.sqrt(self.hidden), (self.hidden, self.dim))
            self.b2 = np.zeros(self.dim)

    def _forward(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        H = np.tanh(X @ self.W1 + self.b1)
        return H, H @ self.W2 + self.b2

    def predict(self, X: np.ndarray) -> np.ndarray:
        X = np.atleast_2d(X)
        _, Y = self._forward(X)
        return _normalize_rows(Y)

    def fit(self, X: np.ndarray, Y: np.ndarray, epochs: int = 300, lr: float = 0.1,
            l2: float = 1e-4) -> float:
        """Градиентный спуск по 1 - cos(pred, Y). Возвращает финальную потерю."""
        X = np.atleast_2d(np.asarray(X, dtype=float))
        T = _normalize_rows(np.atleast_2d(np.asarray(Y, dtype=float)))
        N = X.shape[0]
        loss = float("nan")
        for _ in range(epochs):
            H, P = self._forward(X)
            norm = np.linalg.norm(P, axis=1, keepdims=True)
            norm = np.where(norm == 0, 1e-12, norm)
            Nn = P / norm
            cos = np.sum(Nn * T, axis=1, keepdims=True)
            loss = float(np.mean(1.0 - cos))
            # dL/dP для L = mean(1 - cos): -(T - cos * Nn) / ||P|| / N
            dP = -(T - cos * Nn) / norm / N
            dW2 = H.T @ dP + l2 * self.W2
            db2 = dP.sum(axis=0)
            dH = dP @ self.W2.T
            dpre = dH * (1.0 - H * H)
            dW1 = X.T @ dpre + l2 * self.W1
            db1 = dpre.sum(axis=0)
            self.W2 -= lr * dW2
            self.b2 -= lr * db2
            self.W1 -= lr * dW1
            self.b1 -= lr * db1
        return loss

    def save(self, path: str) -> None:
        np.savez(path, W1=self.W1, b1=self.b1, W2=self.W2, b2=self.b2, dim=self.dim,
                 hidden=self.hidden, report=np.array(repr(self.report)))

    @classmethod
    def load(cls, path: str) -> "TransitionModel":
        d = np.load(path, allow_pickle=False)
        m = cls(dim=int(d["dim"]), hidden=int(d["hidden"]),
                W1=d["W1"], b1=d["b1"], W2=d["W2"], b2=d["b2"])
        return m


def hit_at_k(scores_rows: np.ndarray, target_idx: np.ndarray, k: int) -> float:
    """Доля примеров, у которых верный кандидат попал в top-k по убыванию score."""
    if len(target_idx) == 0:
        return 0.0
    topk = np.argsort(-scores_rows, axis=1)[:, :k]
    return float(np.mean([t in row for t, row in zip(target_idx, topk)]))


def nearest_candidate(Y: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    """Индекс ближайшего кандидата (косинус) для каждого целевого вектора."""
    return np.argmax(_normalize_rows(Y) @ _normalize_rows(candidates).T, axis=1)


def fit_with_gate(X: np.ndarray, Y: np.ndarray, candidates: np.ndarray, k: int = 5,
                  holdout: float = 0.2, margin: float = 0.05, seed: int = 0,
                  epochs: int = 300) -> Tuple[Optional[TransitionModel], Dict]:
    """Обучает на части данных, сравнивает с популярностью на остальной части.

    Возвращает (model, report). model is None, если модель не выиграла у базового уровня.
    """
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    n = len(X)
    report: Dict = {"n": n, "k": k}
    if n < 20 or len(candidates) < k + 1:
        report.update(accepted=False, reason="мало данных")
        return None, report
    rng = np.random.default_rng(seed)
    order = rng.permutation(n)
    n_test = max(5, int(n * holdout))
    test_idx, train_idx = order[:n_test], order[n_test:]
    target = nearest_candidate(Y, candidates)

    # базовый уровень: популярность целевых концептов на обучающей части
    counts = np.bincount(target[train_idx], minlength=len(candidates)).astype(float)
    baseline_scores = np.tile(counts, (len(test_idx), 1))
    base_hit = hit_at_k(baseline_scores, target[test_idx], k)

    model = TransitionModel(dim=X.shape[1], hidden=min(64, max(8, X.shape[1] // 4)), seed=seed)
    loss = model.fit(X[train_idx], Y[train_idx], epochs=epochs)
    cand_n = _normalize_rows(candidates)
    model_scores = model.predict(X[test_idx]) @ cand_n.T
    model_hit = hit_at_k(model_scores, target[test_idx], k)

    accepted = model_hit >= base_hit + margin
    report.update(train_loss=round(loss, 4), model_hit=round(model_hit, 4),
                  baseline_hit=round(base_hit, 4), accepted=bool(accepted))
    model.report = report
    return (model if accepted else None), report
