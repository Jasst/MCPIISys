"""Тормозные взаимодействия: чистые функции без numpy/torch (тестируются отдельно).

- lateral_rerank: латеральное торможение между кандидатами выдачи. Почти
  одинаковые результаты гасят друг друга, поэтому в top-k попадает разнообразие,
  а не десять дублей одного факта. Набор кандидатов не меняется, меняется порядок.
- inhibitory_delta / clamp_inhibitory: правило iSTDP-гомеостаза для тормозных
  синапсов. Тормоз усиливается у узлов, активнее целевого уровня, и ослабляется у тихих.
"""
from typing import Callable, Dict, List, Optional, Sequence


def lateral_penalty(sim: float, threshold: float, lam: float, strong_score: float) -> float:
    """Штраф слабому кандидату за сходство с более сильным (уже выбранным).

    0, если сходство не выше порога. Иначе растёт линейно до lam * strong_score
    при sim = 1.0. Линейная шкала не даёт резкого скачка на границе порога.
    """
    if threshold >= 1.0 or sim <= threshold or lam <= 0.0:
        return 0.0
    overlap = min(1.0, (sim - threshold) / (1.0 - threshold))
    return lam * overlap * max(0.0, strong_score)


def lateral_rerank(items: Sequence[Dict], similarity: Callable[[int, int], float],
                   threshold: float = 0.9, lam: float = 0.3, score_key: str = "score") -> List[Dict]:
    """Жадный отбор с латеральным торможением.

    items: список словарей с полем score_key (базовый скор, по убыванию не обязательно).
    similarity(i, j): косинус между items[i] и items[j] по их индексам в исходном списке.
    На каждом шаге выбирается кандидат с максимальным скором после штрафа за уже
    выбранных. Возвращает новый список тех же элементов в порядке отбора.
    Исходные словари не меняются, добавляется только ключ 'score_adj'.
    """
    n = len(items)
    if n == 0:
        return []
    remaining = list(range(n))
    selected: List[int] = []
    result: List[Dict] = []
    while remaining:
        best_idx: Optional[int] = None
        best_adj = float("-inf")
        for r in remaining:
            base = float(items[r].get(score_key, 0.0))
            penalty = 0.0
            for s in selected:
                p = lateral_penalty(similarity(s, r), threshold, lam,
                                    float(items[s].get(score_key, 0.0)))
                if p > penalty:
                    penalty = p
            adj = base - penalty
            if adj > best_adj:
                best_adj = adj
                best_idx = r
        selected.append(best_idx)
        remaining.remove(best_idx)
        out = dict(items[best_idx])
        out["score_adj"] = best_adj
        result.append(out)
    return result


def inhibitory_delta(post_rate: float, target_rate: float, eta: float) -> float:
    """Изменение веса тормозного синапса за один проход гомеостаза."""
    return eta * (post_rate - target_rate)


def clamp_inhibitory(weight: float, max_weight: float) -> float:
    """Тормозный вес держим в [0, max_weight]: знак задаётся полем sign, не весом."""
    return max(0.0, min(max_weight, weight))
