"""
Ядро просмотра памяти: снимок для графа, детали узла, правки и подготовка проверки LLM.

Работает с объектом MemoryStore, не знает о FastAPI, сессиях и путях к файлам.
Используется маршрутами routes/memory_viewer.py (внутри приложения), поэтому правки
проходят через уже запущенный сервис памяти пользователя.
"""
import json
import math
import re
from collections import Counter, defaultdict, deque
from datetime import timezone

import numpy as np

ID_RE = re.compile(r"^[A-Za-z0-9_.@:+\-]{1,128}$")
REL_RE = re.compile(r"^[A-Z][A-Z_]{1,31}$")
CONTRADICT_RELATIONS = ("CONTRADICTS", "CONTRADICT")
ACTOR = "memory_viewer"
MAX_NODES_DEFAULT = 800


def _utc(dt):
    """Время без часового пояса (старые записи) приводим к UTC, чтобы сравнивать с aware-датами."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _conf(o):
    """Доверие как число: None или мусор в старых данных не должны ронять снимок."""
    try:
        v = float(o.confidence)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _scope(o):
    return getattr(o.scope, "value", str(o.scope))


def _kind(o):
    """Вид записи (fact_type из метаданных), если есть; иначе тип из перечисления."""
    meta = o.object if isinstance(o.object, dict) else {}
    return meta.get("fact_type") or o.type.value


def store_version(store):
    """Дешёвая версия содержимого: меняется при любой правке или добавлении."""
    with store._lock:  # RLock: снимок может строиться в другом потоке, пока чат пишет в память
        objs = store._objects.values()
        return f"{len(store._objects)}:{len(store._events)}:{sum(o.version for o in objs)}"


def build_snapshot(store, readonly=False, max_nodes=MAX_NODES_DEFAULT, with_pca=True):
    """Снимок графа. Берёт блокировку хранилища, поэтому безопасен в отдельном потоке.
    with_pca=False пропускает SVD-проекцию (нужна только для раскладки PCA)."""
    with store._lock:
        return _build_snapshot_locked(store, readonly, max_nodes, with_pca)


def _build_snapshot_locked(store, readonly, max_nodes, with_pca):
    objs = store._objects
    g = store._graph

    edges = []
    degree = defaultdict(int)
    adj = defaultdict(set)
    for src, lst in g._edges.items():
        for rel, tgt in lst:
            if src in objs and tgt in objs:
                w = g.get_relation_weight(src, rel, tgt) or 1.0
                edges.append({"s": src, "r": rel, "t": tgt, "w": round(float(w), 3)})
                degree[src] += 1
                degree[tgt] += 1
                adj[src].add(tgt)
                adj[tgt].add(src)

    ranked = sorted(objs.keys(), key=lambda k: (degree[k], _conf(objs[k])), reverse=True)
    shown = ranked[:max_nodes]
    shown_set = set(shown)

    nodes = []
    for oid in shown:
        o = objs[oid]
        meta = o.object if isinstance(o.object, dict) else {}
        nodes.append({
            "id": oid,
            "type": _kind(o),
            "subject": o.subject,
            "predicate": o.predicate,
            "confidence": round(_conf(o), 4),
            "version": o.version,
            "scope": _scope(o),
            "author": o.author,
            "created": _utc(o.created).isoformat(),
            "evidence": len(o.evidence or []),
            "source_type": o.source_type,
            "last_accessed": meta.get("last_accessed"),
            "degree": degree[oid],
        })
    edges = [e for e in edges if e["s"] in shown_set and e["t"] in shown_set]

    return {
        "version": store_version(store),
        "readonly": readonly,
        "total_nodes": len(objs),
        "shown_nodes": len(nodes),
        "nodes": nodes,
        "edges": edges,
        "pca": _pca(store, shown_set) if with_pca else {},
        "metrics": _metrics(store, degree, adj),
    }


def _pca(store, allowed):
    emb = {k: v for k, v in store._embedding_index.items() if k in allowed}
    # берём только основную размерность: один «кривой» вектор не должен ломать проекцию всех
    if emb:
        dim = Counter(len(v) for v in emb.values()).most_common(1)[0][0]
        emb = {k: v for k, v in emb.items() if len(v) == dim}
    if len(emb) < 3:
        return {}
    ids = list(emb.keys())
    X = np.array([emb[i] for i in ids], dtype=float)
    X = X - X.mean(axis=0)
    try:
        _, _, vt = np.linalg.svd(X, full_matrices=False)
    except np.linalg.LinAlgError:
        return {}
    coords = X @ vt[:2].T
    if coords.shape[1] < 2:
        coords = np.hstack([coords, np.zeros((coords.shape[0], 2 - coords.shape[1]))])
    scale = np.max(np.abs(coords)) or 1.0
    coords = coords / scale
    return {i: [round(float(x), 4), round(float(y), 4)] for i, (x, y) in zip(ids, coords)}


def _metrics(store, degree, adj):
    objs = store._objects
    g = store._graph
    confs = [_conf(o) for o in objs.values()]
    by_type = Counter(_kind(o) for o in objs.values())
    by_scope = Counter(_scope(o) for o in objs.values())
    rel_counts = Counter()
    contradictions = 0
    for src, lst in g._edges.items():
        for rel, _tgt in lst:
            rel_counts[rel] += 1
            if rel in CONTRADICT_RELATIONS:
                contradictions += 1
    contradictions //= 2  # рёбра двунаправленные

    hist = [0] * 10
    for c in confs:
        hist[min(9, max(0, int(c * 10)))] += 1

    seen, components = set(), 0
    for oid in objs:
        if oid in seen:
            continue
        components += 1
        seen.add(oid)
        q = deque([oid])
        while q:
            cur = q.popleft()
            for nb in adj.get(cur, ()):
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)

    hubs = sorted(objs.keys(), key=lambda k: degree.get(k, 0), reverse=True)[:8]
    created = sorted(_utc(o.created) for o in objs.values()) if objs else []
    n = len(objs)
    return {
        "nodes": n,
        "edges": sum(rel_counts.values()),
        "events": len(store._events),
        "embeddings": len(store._embedding_index),
        "components": components,
        "isolated": sum(1 for k in objs if degree.get(k, 0) == 0),
        "contradictions": contradictions,
        "avg_confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
        "avg_degree": round(sum(degree.values()) / n, 3) if n else 0.0,
        "avg_evidence": round(sum(len(o.evidence or []) for o in objs.values()) / n, 3) if n else 0.0,
        "by_type": dict(by_type.most_common()),
        "by_scope": dict(by_scope.most_common()),
        "relations": dict(rel_counts.most_common()),
        "confidence_hist": hist,
        "hubs": [{"id": k, "subject": objs[k].subject, "degree": degree.get(k, 0)} for k in hubs],
        "oldest": created[0].isoformat() if created else None,
        "newest": created[-1].isoformat() if created else None,
    }


def object_detail(store, oid):
    o = store._objects.get(oid)
    if o is None:
        raise LookupError("объект не найден")
    g = store._graph
    out_edges = [{"r": r, "t": t, "subject": store._objects[t].subject if t in store._objects else None}
                 for r, t in g.get_neighbors(oid)]
    in_edges = [{"r": r, "s": s, "subject": store._objects[s].subject if s in store._objects else None}
                for r, s in g.get_incoming(oid)]
    events = []
    for e in store.get_object_history(oid)[-30:]:
        payload = getattr(e, "payload", None)
        if isinstance(payload, dict):
            payload = {k: str(v)[:300] for k, v in payload.items()}
        else:
            payload = str(payload)[:300]
        events.append({
            "type": getattr(e.type, "value", str(e.type)),
            "actor": getattr(e, "actor", None),
            "timestamp": e.timestamp.isoformat() if hasattr(e, "timestamp") else None,
            "payload": payload,
        })
    return {
        "id": oid,
        "type": _kind(o),
        "subject": o.subject,
        "predicate": o.predicate,
        "object": str(o.object)[:2000],
        "confidence": _conf(o),
        "version": o.version,
        "scope": _scope(o),
        "author": o.author,
        "created": _utc(o.created).isoformat(),
        "evidence": list(o.evidence or [])[:50],
        "source_type": o.source_type,
        "has_embedding": store.get_embedding(oid) is not None,
        "out_edges": out_edges,
        "in_edges": in_edges,
        "events": events,
    }


def apply_edit(store, op, args, actor=ACTOR):
    """Меняет память через публичные методы MemoryStore. Бросает ValueError при неверных данных."""
    args = args or {}

    def need(key):
        v = args.get(key)
        if not isinstance(v, str) or not ID_RE.match(v):
            raise ValueError(f"некорректный id: {key}")
        if v not in store._objects:
            raise ValueError("объект не найден")
        return v

    def text(key, limit=1000):
        v = args.get(key)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"пустое поле: {key}")
        return v.strip()[:limit]

    def rel(key="relation"):
        v = args.get(key)
        if not isinstance(v, str) or not REL_RE.match(v):
            raise ValueError("отношение: заглавные буквы и подчёркивания, например RELATED_TO")
        return v

    def unit(v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise ValueError("ожидается число от 0 до 1")
        if not math.isfinite(x):
            raise ValueError("ожидается число от 0 до 1")
        return max(0.0, min(1.0, x))

    if op == "create":
        subject = text("subject")
        try:
            conf = unit(args.get("confidence", 0.5))
        except ValueError:
            raise ValueError("доверие: ожидается число от 0 до 1")
        predicate = None
        if isinstance(args.get("predicate"), str) and args["predicate"].strip():
            predicate = args["predicate"].strip()[:200]
        # штатный путь добавления факта (тот же, что у чата): дедупликация и индексы
        oid = store.add_fact(subject, "claim", actor, confidence=conf)
        if predicate:
            store.update(oid, {"predicate": predicate}, actor)
        return {"id": oid}
    if op == "set_subject":
        store.update(need("id"), {"subject": text("text")}, actor)
    elif op == "set_predicate":
        store.update(need("id"), {"predicate": text("text", 200)}, actor)
    elif op == "set_confidence":
        store.update(need("id"), {"confidence": unit(args.get("value"))}, actor)
    elif op == "retract":
        store.retract(need("id"), actor, reason=str(args.get("reason") or "memory_viewer")[:200])
    elif op == "link":
        store.link(need("source"), need("target"), rel(), actor, weight=unit(args.get("weight", 1.0)))
    elif op == "unlink":
        store.unlink(need("source"), need("target"), rel(), actor)
    elif op == "set_weight":
        store.set_relation_weight(need("source"), need("target"), rel(), unit(args.get("weight")), actor)
    else:
        raise ValueError(f"неизвестная операция: {op}")


def review_messages(detail):
    neighbours = [f"- {n.get('subject')!s} ({n.get('r')})" for n in
                  (detail["out_edges"] + detail["in_edges"])[:8]]
    prompt = (
        "Проверь одну запись долговременной памяти ассистента.\n"
        f"Тип: {detail['type']}\nТекст: {detail['subject']}\n"
        f"Предикат: {detail['predicate']}\nДоверие: {detail['confidence']}\n"
        "Связи:\n" + ("\n".join(neighbours) or "- нет") + "\n\n"
        "Ответь ТОЛЬКО JSON без пояснений и без markdown:\n"
        '{"verdict": "ok|dubious|wrong|duplicate|unclear", "reason": "кратко", '
        '"suggested_subject": null или новый текст, '
        '"suggested_confidence": null или число от 0 до 1, '
        '"suggest_retract": false}'
    )
    return [
        {"role": "system", "content": "Ты аудитор памяти. Отвечаешь строго JSON."},
        {"role": "user", "content": prompt},
    ]


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def sanitize_proposal(p):
    """Белый список полей: ответ модели не применяется напрямую и не доверяется слепо."""
    if not isinstance(p, dict):
        p = {}
    verdict = p.get("verdict")
    out = {
        "verdict": verdict if verdict in ("ok", "dubious", "wrong", "duplicate", "unclear") else "unclear",
        "reason": str(p.get("reason") or "")[:400],
        "suggested_subject": None,
        "suggested_confidence": None,
        "suggest_retract": bool(p.get("suggest_retract")),
    }
    s = p.get("suggested_subject")
    if isinstance(s, str) and s.strip():
        out["suggested_subject"] = s.strip()[:1000]
    c = p.get("suggested_confidence")
    if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c):
        out["suggested_confidence"] = max(0.0, min(1.0, float(c)))
    return out


def edge_key(e):
    return f"{e['s']}|{e['r']}|{e['t']}"


def diff_snapshots(prev, cur):
    """Дельта между двумя снимками build_snapshot. Клиент применяет её к состоянию версии base."""
    prev_nodes = {n["id"]: n for n in prev["nodes"]}
    cur_nodes = {n["id"]: n for n in cur["nodes"]}
    prev_edges = {edge_key(e): e for e in prev["edges"]}
    cur_edges = {edge_key(e): e for e in cur["edges"]}
    return {
        "delta": True,
        "base": prev["version"],
        "version": cur["version"],
        "readonly": cur["readonly"],
        "total_nodes": cur["total_nodes"],
        "shown_nodes": cur["shown_nodes"],
        "nodes_added": [n for k, n in cur_nodes.items() if k not in prev_nodes],
        "nodes_changed": [n for k, n in cur_nodes.items()
                          if k in prev_nodes and n != prev_nodes[k]],
        "nodes_removed": [k for k in prev_nodes if k not in cur_nodes],
        "edges_added": [e for k, e in cur_edges.items() if prev_edges.get(k) != e],
        "edges_removed": [k for k in prev_edges if k not in cur_edges],
        "pca": cur["pca"],
        "metrics": cur["metrics"],
    }