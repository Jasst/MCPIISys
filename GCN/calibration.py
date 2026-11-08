"""Калибровка порогов по размеченным парам текстов.

Порог выбирается максимизацией F1 на размеченной выборке, а не угадыванием.
Результат хранится в GCN/calibration.json и подхватывается config_ai.py.
"""
import json
import os
from typing import Dict, Iterable, List, Optional, Tuple


def best_threshold(scores: Iterable[float], labels: Iterable[bool]) -> Tuple[float, float, float, float]:
    """Порог t: предсказываем «совпадение», если score >= t.

    Возвращает (threshold, f1, precision, recall). Перебираются все значения score.
    Сложность O(n^2) на выборке; для десятков тысяч пар стоит перейти на сортировку.
    """
    pairs = [(float(s), bool(l)) for s, l in zip(scores, labels)]
    if not pairs:
        raise ValueError("пустая выборка")
    candidates = sorted({s for s, _ in pairs})
    best = (candidates[0], -1.0, 0.0, 0.0)
    for t in candidates:
        tp = sum(1 for s, l in pairs if s >= t and l)
        fp = sum(1 for s, l in pairs if s >= t and not l)
        fn = sum(1 for s, l in pairs if s < t and l)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        if f1 > best[1] + 1e-12:
            best = (t, f1, precision, recall)
    return best


def load_calibration(path: str) -> Dict[str, float]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (OSError, ValueError, TypeError):
        return {}


def save_calibration(path: str, values: Dict[str, float]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(values, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
