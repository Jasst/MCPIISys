"""Правила пластичности синапсов в виде чистых функций (без numpy/faiss).

Вынесено из memory_graph.py, чтобы правила проверялись юнит-тестами без
тяжёлых зависимостей. memory_graph использует эти функции при обновлении весов.
"""
from dataclasses import dataclass
from typing import Dict, Hashable, Set, Tuple


@dataclass(frozen=True)
class PlasticityParams:
    ltp: float = 0.01               # STDP_LONG_TERM_POTENTIATION
    ltd: float = 0.005              # STDP_LONG_TERM_DEPRESSION
    window: float = 5.0             # STDP_TIME_WINDOW, секунды
    base_rate: float = 0.02         # HEBBIAN_LEARNING_RATE
    floor: float = 0.05             # минимальная пластичность
    stabilize_k: float = 0.1        # скорость стабилизации по числу подтверждений
    initial_plasticity: float = 0.5


def stdp_delta(dt: float, plasticity: float, p: PlasticityParams) -> float:
    """Изменение веса синапса пре->пост по правилу STDP.

    dt = t_post - t_pre в секундах (моменты реальных событий обоих узлов).
      dt > 0, |dt| < window: пре предшествовал посту -> потенциация,
                             величина убывает с ростом |dt|.
      dt < 0, |dt| < window: пост предшествовал пре -> депрессия.
      иначе: без причинного порядка, только слабый прирост как счётчик
             совместной активности (не правило времени).
    """
    plasticity = max(0.0, plasticity)
    if 0.0 < dt < p.window:
        return p.ltp * (1.0 - dt / p.window) * plasticity
    if -p.window < dt < 0.0:
        return -p.ltd * (1.0 - (-dt) / p.window) * plasticity
    return p.base_rate * 0.1 * plasticity


def stabilized_plasticity(coactivation_count: int, p: PlasticityParams) -> float:
    """Метапластичность: часто подтверждаемые связи постепенно стабилизируются.

    Пластичность = initial / (1 + k * n), не ниже floor. Новые связи (n мал)
    остаются пластичными, давно подтверждённые почти не размываются шумом.
    """
    n = max(0, int(coactivation_count))
    return max(p.floor, p.initial_plasticity / (1.0 + p.stabilize_k * n))


def normalize_strength(weights: Dict[Tuple[Hashable, Hashable], float],
                       cap: float, min_weight: float = 0.0) -> Set[Tuple[Hashable, Hashable]]:
    """Синаптическое масштабирование (на месте).

    Суммарная входящая сила узла и суммарная исходящая сила не превышают cap.
    Сначала проход по входящим (группировка по target), затем по исходящим
    (по source). Возвращает ключи синапсов, чьи веса изменились.
    """
    changed: Set[Tuple[Hashable, Hashable]] = set()
    if cap <= 0:
        return changed
    for group_index in (1, 0):  # 1 -> входящие связи узла, 0 -> исходящие
        sums: Dict[Hashable, float] = {}
        members: Dict[Hashable, list] = {}
        for key, w in weights.items():
            g = key[group_index]
            sums[g] = sums.get(g, 0.0) + w
            members.setdefault(g, []).append(key)
        for g, total in sums.items():
            if total <= cap:
                continue
            factor = cap / total
            for key in members[g]:
                new_w = max(min_weight, weights[key] * factor)
                if abs(new_w - weights[key]) > 1e-12:
                    weights[key] = new_w
                    changed.add(key)
    return changed
