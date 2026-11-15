"""Стабилизатор памяти: измеряет граф, предлагает ограниченные правки параметров,
а ИИ-рецензент может только одобрить или отклонить каждое предложение.

Что стабилизатор НЕ делает:
- не удаляет факты и синапсы;
- не создаёт новых действий сверх заложенного набора правил;
- не выходит за BOUNDS и не меняет параметр больше чем на MAX_STEP за цикл;
- не применяет правки, если ИИ-ответ не разобран (по умолчанию: отклонить всё).

Изменённые параметры хранятся в stabilizer_state.json и видны в логе
stabilizer_log.jsonl. Удаление файла возвращает значения по умолчанию.
"""
import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_TUNE: Dict[str, float] = {
    "lateral_lambda": 0.30,      # сила латерального торможения в выдаче
    "node_strength_cap": 3.0,    # предел суммарной входящей силы узла (SYNAPSE_NODE_STRENGTH_CAP)
}
BOUNDS: Dict[str, Tuple[float, float]] = {
    "lateral_lambda": (0.0, 0.6),
    "node_strength_cap": (1.0, 5.0),
}
LAMBDA_STEP = 0.03        # максимум изменения lateral_lambda за цикл (10% от default)
CAP_FACTOR = 0.9          # множитель node_strength_cap за цикл (-10%)
HUB_SHARE_MAX = 0.20      # доля входящей силы одного узла, выше которой это хаб
ACTIVE_FRAC_MAX = 0.30    # доля активных узлов, выше которой выдача шумная
ACTIVE_FRAC_MIN = 0.05    # ниже которой торможение избыточно


@dataclass
class Metrics:
    n_nodes: int = 0
    n_excit: int = 0
    n_inh: int = 0
    inh_ratio: float = 0.0
    hub_share: float = 0.0
    frac_active: float = 0.0
    mean_degree: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def compute_metrics(edges: Iterable[Tuple[int, int, float, int]], active_fraction: float) -> Metrics:
    """edges: (src, tgt, weight, sign). active_fraction: доля узлов с активацией > порога."""
    m = Metrics()
    incoming: Dict[int, float] = {}
    nodes = set()
    total_excit_w = 0.0
    for src, tgt, w, sign in edges:
        nodes.add(src)
        nodes.add(tgt)
        if sign < 0:
            m.n_inh += 1
        else:
            m.n_excit += 1
            incoming[tgt] = incoming.get(tgt, 0.0) + max(0.0, w)
            total_excit_w += max(0.0, w)
    total_edges = m.n_excit + m.n_inh
    m.n_nodes = len(nodes)
    m.inh_ratio = (m.n_inh / total_edges) if total_edges else 0.0
    m.hub_share = (max(incoming.values()) / total_excit_w) if total_excit_w > 0 else 0.0
    m.frac_active = float(active_fraction)
    m.mean_degree = (2.0 * total_edges / m.n_nodes) if m.n_nodes else 0.0
    return m


def _clamp(name: str, value: float) -> float:
    lo, hi = BOUNDS[name]
    return max(lo, min(hi, value))


def propose(metrics: Metrics, tune: Dict[str, float]) -> List[Dict[str, Any]]:
    """Детерминированные предложения. Каждое — одна правка одного параметра."""
    proposals: List[Dict[str, Any]] = []
    cap = tune["node_strength_cap"]
    lam = tune["lateral_lambda"]

    if metrics.n_excit > 0 and metrics.hub_share > HUB_SHARE_MAX:
        new = _clamp("node_strength_cap", cap * CAP_FACTOR)
        if new != cap:
            proposals.append({"id": len(proposals), "param": "node_strength_cap",
                              "old": cap, "new": round(new, 4),
                              "reason": f"хаб: доля входящей силы {metrics.hub_share:.2f}"})
    if metrics.frac_active > ACTIVE_FRAC_MAX:
        new = _clamp("lateral_lambda", lam + LAMBDA_STEP)
        if new != lam:
            proposals.append({"id": len(proposals), "param": "lateral_lambda",
                              "old": lam, "new": round(new, 4),
                              "reason": f"шумная выдача: активно {metrics.frac_active:.2f}"})
    elif metrics.frac_active < ACTIVE_FRAC_MIN and lam > DEFAULT_TUNE["lateral_lambda"]:
        new = _clamp("lateral_lambda", lam - LAMBDA_STEP)
        if new != lam:
            proposals.append({"id": len(proposals), "param": "lateral_lambda",
                              "old": lam, "new": round(new, 4),
                              "reason": "торможение избыточно, возвращаем к базовому"})
    return proposals


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = re.sub(r"```(?:json)?|```", "", raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_review(raw: str, proposals: List[Dict[str, Any]]) -> List[int]:
    """Возвращает id одобренных предложений. Ошибка разбора -> пустой список.

    Ожидается: {"decisions": [{"id": 0, "approve": true}, ...]}.
    Неизвестные id игнорируются, всё, что не одобрено явно, отклоняется.
    """
    data = _extract_json(raw)
    if not data or not isinstance(data.get("decisions"), list):
        return []
    known = {p["id"] for p in proposals}
    approved: List[int] = []
    for d in data["decisions"]:
        if not isinstance(d, dict):
            continue
        pid = d.get("id")
        if pid in known and d.get("approve") is True and pid not in approved:
            approved.append(pid)
    return approved


def apply_approved(tune: Dict[str, float], proposals: List[Dict[str, Any]],
                   approved_ids: List[int]) -> Dict[str, float]:
    """Применяет только одобренные правки, с повторным ограничением границ и шага."""
    new_tune = dict(tune)
    for p in proposals:
        if p["id"] not in approved_ids:
            continue
        name = p["param"]
        if name not in BOUNDS:
            continue
        old = float(new_tune.get(name, DEFAULT_TUNE[name]))
        target = float(p["new"])
        step = LAMBDA_STEP if name == "lateral_lambda" else abs(old) * (1 - CAP_FACTOR) + 1e-9
        target = max(old - step, min(old + step, target))
        new_tune[name] = _clamp(name, target)
    return new_tune


def load_state(path: str) -> Dict[str, float]:
    tune = dict(DEFAULT_TUNE)
    if not os.path.exists(path):
        return tune
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for k in DEFAULT_TUNE:
            if k in data:
                tune[k] = _clamp(k, float(data[k]))
    except (OSError, ValueError, TypeError):
        return dict(DEFAULT_TUNE)
    return tune


def save_state(path: str, tune: Dict[str, float]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({k: tune[k] for k in DEFAULT_TUNE}, fh, ensure_ascii=False)
    os.replace(tmp, path)


def append_log(path: str, record: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


REVIEW_PROMPT = (
    "Ты рецензент стабилизатора памяти. Тебе даны метрики графа и предложенные "
    "правки параметров. Одобряй правку только если она явно обоснована метриками. "
    "Ответь ТОЛЬКО JSON без пояснений: "
    '{{"decisions": [{{"id": <число>, "approve": true|false}}]}}. '
    "Если сомневаешься — approve=false.\n\n"
    "МЕТРИКИ: {metrics}\n\nПРАВКИ: {proposals}"
)


def build_review_prompt(metrics: Metrics, proposals: List[Dict[str, Any]]) -> str:
    return REVIEW_PROMPT.format(
        metrics=json.dumps(metrics.to_dict(), ensure_ascii=False),
        proposals=json.dumps(proposals, ensure_ascii=False),
    )
