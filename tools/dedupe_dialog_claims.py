"""
Одноразовая очистка дублей реплик в gcn_state.json — БЕЗ потери связей.

Старый код для каждой реплики создавал два объекта:
  * MEMORY_EVENT (эпизод, predicate=user_message / assistant_message);
  * CLAIM (локальный факт, fact_type=user / assistant) с тем же текстом.

Скрипт НЕ удаляет связи. Для каждого CLAIM-дубля он находит соответствующий
эпизод и переносит все его рёбра (синапсы, replied_with и т.д.) на эпизод,
сохраняя веса. Затем удаляет сам CLAIM-дубль. Эмбеддинг дубля переносится,
если у эпизода его нет.

Правила переноса рёбер:
  * ребро A -r-> B, где A или B — дубль, становится ребром на эпизод;
  * если после переноса возникает дубликат (тот же src, rel, tgt), остаётся
    ребро с большим весом;
  * если перенос создаёт самопетлю (src == tgt), она отбрасывается;
  * рёбра, которые не касаются дублей, не меняются.

Использование:
    python tools/dedupe_dialog_claims.py путь/к/gcn_state.json [--dry-run]

Перед записью создаётся резервная копия с временной меткой (старые копии
никогда не перезаписываются).
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

EPISODE_PREDICATE = {"user": "user_message", "assistant": "assistant_message"}


def find_duplicates(objects):
    """Возвращает {claim_id: episode_id} для CLAIM-дублей реплик."""
    episodes = {}
    for oid, o in objects.items():
        if o.get("type") == "memory_event":
            episodes.setdefault((o.get("subject"), o.get("author"), o.get("predicate")), oid)

    mapping = {}
    for oid, o in objects.items():
        if o.get("type") != "claim":
            continue
        meta = o.get("object") if isinstance(o.get("object"), dict) else {}
        ftype = meta.get("fact_type")
        if ftype not in EPISODE_PREDICATE:
            continue
        ep = episodes.get((o.get("subject"), o.get("author"), EPISODE_PREDICATE[ftype]))
        if ep is not None:
            mapping[oid] = ep
    return mapping


def remap_edges(data, mapping):
    """Переносит рёбра с дублей на эпизоды, сохраняя веса и убирая дубликаты."""
    m = lambda x: mapping.get(x, x)
    meta_in = data.get("graph_edge_meta", {})

    # Собираем все рёбра в плоский список (src, rel, tgt, weight_meta)
    flat = []
    for src, lst in data.get("graph_edges", {}).items():
        for rel, tgt in lst:
            key = f"{src}|{rel}|{tgt}"
            flat.append((src, rel, tgt, meta_in.get(key)))

    best = {}  # (s, rel, t) -> (weight, meta_value)
    dropped_loops = 0
    for src, rel, tgt, mv in flat:
        s, t = m(src), m(tgt)
        if s == t and (s != src or t != tgt):
            dropped_loops += 1  # самопетля, созданная переносом
            continue
        w = float(mv.get("weight", 0.0)) if isinstance(mv, dict) else 0.0
        k = (s, rel, t)
        if k not in best or w > best[k][0]:
            best[k] = (w, mv)

    new_edges, new_meta = {}, {}
    for (s, rel, t), (w, mv) in best.items():
        new_edges.setdefault(s, []).append([rel, t])
        if mv is not None:
            new_meta[f"{s}|{rel}|{t}"] = mv
    return new_edges, new_meta, len(flat), len(best), dropped_loops


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = Path(sys.argv[1])
    dry_run = "--dry-run" in sys.argv

    data = json.loads(path.read_text(encoding="utf-8"))
    objects = data.get("objects", {})
    mapping = find_duplicates(objects)

    print(f"объектов: {len(objects)}, CLAIM-дублей реплик: {len(mapping)}")
    if not mapping:
        return 0

    new_edges, new_meta, before, after, loops = remap_edges(data, mapping)
    print(f"рёбер до: {before}, после: {after}, отброшено самопетель: {loops}")

    if dry_run:
        return 0

    # Переносим эмбеддинги дублей на эпизоды, если у эпизода их нет
    emb = data.get("embeddings", {})
    for claim_id, ep_id in mapping.items():
        if claim_id in emb and ep_id not in emb:
            emb[ep_id] = emb[claim_id]
        emb.pop(claim_id, None)

    for claim_id in mapping:
        objects.pop(claim_id, None)

    data["graph_edges"] = new_edges
    data["graph_edge_meta"] = new_meta

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.before-dedupe-{stamp}.json")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"готово. резервная копия: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())