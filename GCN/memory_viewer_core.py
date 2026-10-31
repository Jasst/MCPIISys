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
from datetime import datetime, timezone

import numpy as np

ID_RE = re.compile(r"^[A-Za-z0-9_.@:+\-]{1,128}$")
REL_RE = re.compile(r"^[A-Z][A-Z_]{1,31}$")
CONTRADICT_RELATIONS = ("CONTRADICTS", "CONTRADICT")
ACTOR = "memory_viewer"
MAX_NODES_DEFAULT = 800
# пороги для советов «что улучшить» (см. _insights)
INSIGHT_ITEMS = 40        # сколько записей показываем в одном совете
STALE_DAYS = 90           # давно не использовалась...
STALE_CONFIDENCE = 0.5    # ...и доверия меньше этого
LOW_CONFIDENCE = 0.3      # почти не доверяемые
ISOLATED_CONFIDENCE = 0.5 # без связей, но уверенные (такие важнее всего связать)
HUB_DEGREE = 12           # слишком много связей: запись, возможно, слишком общая
# связи, по которым одна запись опирается на другую (см. impact)
SUPPORT_RELS = ("GROUNDS_IN", "REQUIRES", "ENABLES", "CAUSES")
# допустимые типы связей; ИИ может предложить только их
RELATIONS_ALLOWED = ("RELATED_TO", "GROUNDS_IN", "CAUSES", "ENABLES", "REQUIRES",
                     "PREVENTS", "PART_OF", "DUPLICATE_OF", "CONTRADICTS")


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


def _importance(o):
    """Важность записи (meta.importance из add_fact). Мусор и старые данные без поля → 1.0."""
    meta = o.object if isinstance(o.object, dict) else {}
    try:
        v = float(meta.get("importance", 1.0))
    except (TypeError, ValueError):
        return 1.0
    return v if math.isfinite(v) else 1.0


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
    contra_pairs = set()
    for src, lst in g._edges.items():
        for rel, tgt in lst:
            if src in objs and tgt in objs:
                # противоречие хранится в двух направлениях: рисуем и считаем его один раз
                if rel in CONTRADICT_RELATIONS:
                    pk = (min(src, tgt), max(src, tgt), rel)
                    if pk in contra_pairs:
                        continue
                    contra_pairs.add(pk)
                w = g.get_relation_weight(src, rel, tgt) or 1.0
                edges.append({"s": src, "r": rel, "t": tgt, "w": round(float(w), 3)})
                degree[src] += 1
                degree[tgt] += 1
                adj[src].add(tgt)
                adj[tgt].add(src)

    ranked = sorted(objs.keys(), key=lambda k: (degree[k], _conf(objs[k]), _importance(objs[k])), reverse=True)
    shown = ranked[:max_nodes]
    shown_set = set(shown)

    nodes = []
    for oid in shown:
        o = objs[oid]
        meta = o.object if isinstance(o.object, dict) else {}
        imp = _importance(o)
        # «важность для графа»: доверие + важность + частота обращений — по ней рисуются размер и яркость узла
        salience = round(min(1.0, 0.5 * _conf(o) + 0.3 * min(1.0, imp / 2.0)
                             + 0.2 * min(1.0, int(meta.get("access_count") or 0) / 10.0)), 4)
        nodes.append({
            "id": oid,
            "type": _kind(o),
            "subject": o.subject,
            "predicate": o.predicate,
            "confidence": round(_conf(o), 4),
            "importance": round(imp, 4),
            "salience": salience,
            "access_count": int(meta.get("access_count") or 0),
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
        "insights": _insights(store, degree, adj),
    }


def _norm_text(s):
    """Текст для поиска повторов: регистр, знаки препинания и лишние пробелы не важны."""
    return " ".join(re.sub(r"[^\w\s]", " ", str(s or "").lower()).split())


def _parse_dt(v):
    if not v:
        return None
    try:
        return _utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except ValueError:
        return None


def _insights(store, degree, adj):
    """Советы, что улучшить в памяти. Только читает данные: ничего не меняет и не применяет.

    Каждый совет: kind, level (bad / warn / info), title, hint (что делать человеку),
    count (всего записей), items (до INSIGHT_ITEMS записей с текстом для списка)."""
    objs = store._objects
    now = datetime.now(timezone.utc)
    out = []

    def add(kind, level, title, hint, ids):
        uniq = list(dict.fromkeys(i for i in ids if i in objs))
        if not uniq:
            return
        out.append({
            "kind": kind,
            "level": level,
            "title": title,
            "hint": hint,
            "count": len(uniq),
            "ids": uniq[:INSIGHT_ITEMS],
            "items": [{"id": i, "subject": str(objs[i].subject or "")[:160]}
                      for i in uniq[:INSIGHT_ITEMS]],
        })

    # 1. противоречия: пары записей, соединённых CONTRADICT*
    pairs = set()
    for src_id, lst in store._graph._edges.items():
        for rel, tgt in lst:
            if rel in CONTRADICT_RELATIONS and src_id in objs and tgt in objs:
                pairs.add(tuple(sorted((src_id, tgt))))
    add("contradiction", "bad", f"Противоречия: {len(pairs)}",
        "Два утверждения спорят друг с другом. Оставь верное: второе отзови "
        "или понизь его доверие. Ассистент рассуждает хуже, пока противоречие не решено.",
        [i for p in sorted(pairs) for i in p])

    # 2. повторы: одинаковый текст у разных записей
    groups = defaultdict(list)
    for oid, o in objs.items():
        key = _norm_text(o.subject)
        if key:
            groups[key].append(oid)
    dups = [g for g in groups.values() if len(g) > 1]
    add("duplicate", "warn", f"Повторы текста: {len(dups)} групп",
        "Один и тот же факт записан несколько раз. Оставь одну запись, остальные отзови "
        "или свяжи их связью DUPLICATE_OF.",
        [i for g in dups for i in g])

    # 3. уверенные, но без связей: ассистент не может провести через них цепочку рассуждений
    add("isolated", "warn", "Уверенные, но без связей",
        "Такие записи не связаны ни с чем. Свяжи их с родственной записью в карточке "
        "(блок «Добавить связь»), тогда ассистент найдёт цепочки рассуждений.",
        [k for k, o in objs.items()
         if degree.get(k, 0) == 0 and _conf(o) >= ISOLATED_CONFIDENCE])

    # 4. почти не доверяемые
    add("low_confidence", "info", "Почти не доверяемые",
        "Ассистент сам в них сомневается. Подтверди, если верно, или отзови, если устарело.",
        [k for k, o in objs.items() if _conf(o) < LOW_CONFIDENCE])

    # 5. давно не использовались и сомнительны
    stale = []
    for k, o in objs.items():
        base = _utc(o.created)
        if base is None:
            continue
        meta = o.object if isinstance(o.object, dict) else {}
        last = _parse_dt(meta.get("last_accessed")) or base
        if _conf(o) < STALE_CONFIDENCE and (now - last).days >= STALE_DAYS:
            stale.append(k)
    add("stale", "info", "Давно не использовались и сомнительны",
        f"Не использовались больше {STALE_DAYS} дней и доверия мало. "
        "Кандидаты на очистку: проверь и отзови, если они больше не нужны.",
        stale)

    # 6. без вектора: семантический поиск их не находит (только если векторы вообще используются)
    if store._embedding_index:
        add("no_embedding", "info", "Без вектора поиска",
            "Семантический поиск не находит такие записи по смыслу. Сохрани текст "
            "в карточке: вектор пересчитается.",
            [k for k in objs if k not in store._embedding_index])

    # 7. слишком много связей
    add("hub", "info", "Очень много связей",
        "Такая запись, возможно, слишком общая. Лучше разбить её на несколько конкретных.",
        [k for k in objs if degree.get(k, 0) >= HUB_DEGREE])
    return out


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
    meta = o.object if isinstance(o.object, dict) else {}
    return {
        "id": oid,
        "type": _kind(o),
        "subject": o.subject,
        "predicate": o.predicate,
        "object": str(o.object)[:2000],
        "confidence": _conf(o),
        "importance": _importance(o),
        "access_count": int(meta.get("access_count") or 0),
        "last_accessed": meta.get("last_accessed"),
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

    def importance_value(v):
        """Важность вне диапазона доверия: разумный потолок 5.0."""
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise ValueError("важность: ожидается число")
        if not math.isfinite(x):
            raise ValueError("важность: ожидается число")
        return max(0.0, min(5.0, x))

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
        oid = store.add_fact(subject, "claim", actor, confidence=conf,
                             importance=importance_value(args.get("importance", 1.0)))
        if predicate:
            store.update(oid, {"predicate": predicate}, actor)
        return {"id": oid}
    if op == "set_subject":
        store.update(need("id"), {"subject": text("text")}, actor)
    elif op == "set_predicate":
        store.update(need("id"), {"predicate": text("text", 200)}, actor)
    elif op == "set_confidence":
        store.update(need("id"), {"confidence": unit(args.get("value"))}, actor)
    elif op == "set_importance":
        # важность живёт в метаданных записи; update() принимает только поля объекта целиком,
        # поэтому меняем meta через сам объект так, чтобы история события осталась полной
        oid = need("id")
        o = store._objects[oid]
        meta = o.object if isinstance(o.object, dict) else {}
        new_meta = dict(meta)
        new_meta["importance"] = importance_value(args.get("value"))
        store.update(oid, {"object": new_meta}, actor)
    elif op == "retract":
        store.retract(need("id"), actor, reason=str(args.get("reason") or "memory_viewer")[:200])
    elif op == "merge_into":
        # безопасное слияние повторов: «шум» (from_id) отзывается, его связи переводятся на «канон» (into_id).
        # между записями группы остаётся DUPLICATE_OF — так история слияния читается в журнале.
        src, dst = need("from_id"), need("into_id")
        if src == dst:
            raise ValueError("нельзя слить запись саму с собой")
        g = store._graph
        moved = 0

        def relink(s, r, t):
            """Переносит связь s→t так, чтобы одним концом стал dst; дубликаты не создаёт."""
            nonlocal moved
            w = g.get_relation_weight(s, r, t) or 1.0
            store.unlink(s, t, r, actor)
            if r in CONTRADICT_RELATIONS:
                store.unlink(t, s, r, actor)
            ns, nt = (dst, t) if s == src else (s, dst)
            if ns == nt:
                return  # связь внутри группы повторов — после слияния она не нужна
            if g.get_relation_weight(ns, r, nt) is not None:
                return  # у канона уже есть такая связь
            store.link(ns, nt, r, actor, weight=w)
            moved += 1

        for r, t in list(g.get_neighbors(src)):
            if t != src:
                relink(src, r, t)
        for r, s in list(g.get_incoming(src)):
            if s != src:
                relink(s, r, src)
        store.link(src, dst, "DUPLICATE_OF", actor, weight=1.0)
        store.retract(src, actor, reason=f"merged_into:{dst}"[:200])
        return {"merged_into": dst, "moved_edges": moved}
    elif op == "group":
        # объединение записей в группу: новая запись-группа и связь PART_OF от каждой участницы к ней.
        # участницы не меняются; группа — обычная запись, её можно переименовать, проверить или распустить.
        raw = args.get("ids")
        if not isinstance(raw, list) or not (2 <= len(raw) <= 50):
            raise ValueError("для группы нужно от 2 до 50 записей")
        members = []
        for v in raw:
            if not isinstance(v, str) or not ID_RE.match(v) or v not in store._objects:
                raise ValueError("в группе есть некорректная или отсутствующая запись")
            if v not in members:
                members.append(v)
        if len(members) < 2:
            raise ValueError("для группы нужно минимум 2 разные записи")
        name = text("subject", 200)
        gid = store.add_fact(name, "group", actor, confidence=0.8)
        for m in members:
            store.link(m, gid, "PART_OF", actor, weight=1.0)
        return {"id": gid, "members": len(members)}
    elif op == "ungroup":
        # распад группы: сама группа снимается вместе со своими связями, записи-участницы остаются
        gid = need("id")
        meta = store._objects[gid].object
        if not isinstance(meta, dict) or meta.get("fact_type") != "group":
            raise ValueError("распадается только группа")
        store.retract(gid, actor, reason="ungrouped")
        return {"ungrouped": gid}
    elif op == "link":
        r = rel()
        if r in CONTRADICT_RELATIONS:
            # противоречие хранится в двух направлениях: ставим обе стороны сразу
            store.register_contradiction(need("source"), need("target"), actor)
        else:
            store.link(need("source"), need("target"), r, actor, weight=unit(args.get("weight", 1.0)))
    elif op == "unlink":
        r = rel()
        s, t = need("source"), need("target")
        store.unlink(s, t, r, actor)
        if r in CONTRADICT_RELATIONS:
            store.unlink(t, s, r, actor)   # вторая сторона противоречия
    elif op == "edge_replace":
        # смена типа связи одной операцией: старая связь снимается, новая ставится
        s, t = need("source"), need("target")
        old = args.get("old_relation")
        new = args.get("new_relation")
        if not isinstance(old, str) or not REL_RE.match(old):
            raise ValueError("старый тип связи некорректен")
        if not isinstance(new, str) or new not in RELATIONS_ALLOWED:
            raise ValueError("новый тип связи не из списка допустимых")
        if new == old:
            raise ValueError("тип связи не изменился")
        weight = store._graph.get_relation_weight(s, old, t) or 1.0
        store.unlink(s, t, old, actor)
        if old in CONTRADICT_RELATIONS:
            store.unlink(t, s, old, actor)
        if new in CONTRADICT_RELATIONS:
            store.register_contradiction(s, t, actor)
        else:
            store.link(s, t, new, actor, weight=weight)
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


def impact(store, oid):
    """Что изменится, если запись отозвать: от чего она зависит и что держится на ней."""
    o = store._objects.get(oid)
    if o is None:
        raise LookupError("объект не найден")
    g = store._graph

    def subj(x):
        obj = store._objects.get(x)
        return str(obj.subject or "")[:160] if obj else x

    out_e = g.get_neighbors(oid)
    in_e = g.get_incoming(oid)
    return {
        "id": oid,
        "relation_count": len(out_e) + len(in_e),
        "relies_on": sorted({subj(t) for r, t in out_e if r in SUPPORT_RELS}),
        "relied_by": sorted({subj(s) for r, s in in_e if r in SUPPORT_RELS}),
        "contradicts": sorted({subj(t) for r, t in out_e if r in CONTRADICT_RELATIONS}),
    }


def _neighbour_subjects(store, oid, limit=3):
    """До limit соседних записей (для контекста модели): тексты, без служебных id."""
    out = []
    for src, lst in store._graph._edges.items():
        for _rel, tgt in lst:
            if src == oid and tgt in store._objects:
                out.append(tgt)
            elif tgt == oid and src in store._objects:
                out.append(src)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return [str(store._objects[i].subject or "")[:120] for i in out]


def _ctx(store, oid):
    subs = _neighbour_subjects(store, oid)
    return (" (связи: " + "; ".join(f"«{s}»" for s in subs) + ")") if subs else ""


def edge_review_messages(store, source, relation, target):
    a = str(store._objects[source].subject or "")[:500]
    b = str(store._objects[target].subject or "")[:500]
    prompt = (
        "Проверь одну связь в долговременной памяти ассистента.\n"
        f"Запись A: «{a}»{_ctx(store, source)}\n"
        f"Связь: {relation}\n"
        f"Запись B: «{b}»{_ctx(store, target)}\n\n"
        "Смысл связей: GROUNDS_IN — A опирается на B; CAUSES — A вызывает B; "
        "ENABLES — A делает возможным B; REQUIRES — A требует B; PREVENTS — A мешает B; "
        "PART_OF — A часть B; RELATED_TO — связаны по смыслу; DUPLICATE_OF — A и B об одном и том же; "
        "CONTRADICTS — A и B спорят.\n"
        "Реши: связь верна (keep), её нужно удалить (remove) или нужен другой тип (change).\n"
        "Если change, выбери new_relation из списка: " + ", ".join(RELATIONS_ALLOWED) + ".\n\n"
        "Ответь ТОЛЬКО JSON без пояснений и без markdown:\n"
        '{"verdict": "keep|remove|change", "new_relation": null или тип из списка, "reason": "кратко"}'
    )
    return [
        {"role": "system", "content": "Ты аудитор памяти. Отвечаешь строго JSON."},
        {"role": "user", "content": prompt},
    ]


def sanitize_edge(p, relation):
    """Белый список: вердикт и тип связи проверяются, лишнее отбрасывается."""
    if not isinstance(p, dict):
        p = {}
    v = p.get("verdict")
    if v not in ("keep", "remove", "change"):
        v = "unclear"
    new = None
    if v == "change":
        cand = p.get("new_relation")
        if isinstance(cand, str) and cand.strip().upper() in RELATIONS_ALLOWED \
                and cand.strip().upper() != relation:
            new = cand.strip().upper()
        else:
            v = "unclear"
    return {"verdict": v, "new_relation": new, "reason": str(p.get("reason") or "")[:400]}


EDGE_MEANING = (
    "Смысл связей: GROUNDS_IN — A опирается на B; CAUSES — A вызывает B; "
    "ENABLES — A делает возможным B; REQUIRES — A требует B; PREVENTS — A мешает B; "
    "PART_OF — A часть B; RELATED_TO — связаны по смыслу; DUPLICATE_OF — A и B об одном и том же; "
    "CONTRADICTS — A и B спорят."
)


def edges_batch_messages(store, items):
    """Один запрос на несколько связей: модель оценивает каждую по номеру (index)."""
    lines = []
    for i, it in enumerate(items):
        a = str(store._objects[it["source"]].subject or "")[:300]
        b = str(store._objects[it["target"]].subject or "")[:300]
        lines.append(f"{i}. A: «{a}»{_ctx(store, it['source'])} —{it['relation']}→ B: «{b}»{_ctx(store, it['target'])}")
    prompt = (
        "Проверь связи в долговременной памяти ассистента. Каждая связь — по номеру.\n"
        + "\n".join(lines) + "\n\n" + EDGE_MEANING + "\n"
        "Для каждой реши: связь верна (keep), её нужно удалить (remove) или нужен другой тип (change). "
        "Если change, выбери new_relation из списка: " + ", ".join(RELATIONS_ALLOWED) + ".\n\n"
        "Ответь ТОЛЬКО JSON без пояснений и без markdown:\n"
        '{"edges": [{"index": 0, "verdict": "keep|remove|change", "new_relation": null, "reason": "кратко"}]}'
    )
    return [
        {"role": "system", "content": "Ты аудитор памяти. Отвечаешь строго JSON."},
        {"role": "user", "content": prompt},
    ]


def sanitize_edges_batch(p, items):
    """Список вердиктов по порядку items; ответ без нужного индекса — unclear."""
    if not isinstance(p, dict):
        p = {}
    raw = p.get("edges")
    if not isinstance(raw, list):
        raw = []
    out = [{"verdict": "unclear", "new_relation": None, "reason": ""} for _ in items]
    for e in raw[: len(items) * 2]:
        if not isinstance(e, dict):
            continue
        idx = e.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(items):
            continue
        out[idx] = sanitize_edge(e, items[idx]["relation"])
    return out


LINK_SUGGEST_RELATIONS = tuple(r for r in RELATIONS_ALLOWED if r not in CONTRADICT_RELATIONS)


def link_candidates(store, node_id, k=10):
    """Ближайшие по смыслу записи, которые ещё не связаны с node_id. Без LLM."""
    emb = store._embedding_index
    objs = store._objects
    if node_id not in objs or node_id not in emb:
        return []
    linked = set()
    for src, lst in store._graph._edges.items():
        for _rel, tgt in lst:
            if src == node_id:
                linked.add(tgt)
            elif tgt == node_id:
                linked.add(src)
    dim = len(emb[node_id])
    ids = [i for i in objs if i in emb and i != node_id and i not in linked and len(emb[i]) == dim]
    if not ids:
        return []
    mat = np.asarray([emb[i] for i in ids], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0.0] = 1.0
    mat = mat / norms[:, None]
    v = np.asarray(emb[node_id], dtype=np.float32)
    vn = float(np.linalg.norm(v))
    if vn == 0.0:
        return []
    sims = mat @ (v / vn)
    order = np.argsort(-sims)[:k]
    return [{"id": ids[j], "subject": str(objs[ids[j]].subject or "")[:200],
             "similarity": round(float(sims[j]), 3)} for j in order]


def link_suggest_messages(store, node_id, candidates):
    a = str(store._objects[node_id].subject or "")[:400]
    lines = [f"- id={c['id']} | «{c['subject'][:200]}»" for c in candidates]
    prompt = (
        "Запись A из долговременной памяти ассистента:\n"
        f"«{a}»{_ctx(store, node_id)}\n\n"
        "Кандидаты (можно использовать только их id):\n" + "\n".join(lines) + "\n\n"
        + EDGE_MEANING + "\n"
        "Предложи связи между A и кандидатами, только если связь действительно верна по смыслу. "
        "direction: out — A → кандидат, in — кандидат → A. CONTRADICTS не предлагай. "
        "Пустой список — нормальный ответ.\n"
        "relation — из списка: " + ", ".join(LINK_SUGGEST_RELATIONS) + ".\n"
        "Ответь ТОЛЬКО JSON без пояснений и без markdown:\n"
        '{"links": [{"id": "...", "direction": "out|in", "relation": "...", "reason": "кратко"}]}'
    )
    return [
        {"role": "system", "content": "Ты аудитор памяти. Отвечаешь строго JSON."},
        {"role": "user", "content": prompt},
    ]


def sanitize_link_suggestions(p, node_id, candidates):
    """Белый список: только id из кандидатов, известные направления и типы связей."""
    if not isinstance(p, dict):
        p = {}
    raw = p.get("links")
    if not isinstance(raw, list):
        raw = []
    valid = {c["id"]: c for c in candidates}
    out, seen = [], set()
    for x in raw[:20]:
        if not isinstance(x, dict):
            continue
        cid = x.get("id")
        if cid not in valid or cid == node_id:
            continue
        direction = x.get("direction")
        if direction not in ("out", "in"):
            continue
        rel = str(x.get("relation") or "").strip().upper()
        if rel not in LINK_SUGGEST_RELATIONS:
            continue
        key = (cid, direction, rel)
        if key in seen:
            continue
        seen.add(key)
        source, target = (node_id, cid) if direction == "out" else (cid, node_id)
        out.append({"source": source, "target": target, "relation": rel, "direction": direction,
                    "candidate": cid, "subject": valid[cid]["subject"],
                    "reason": str(x.get("reason") or "")[:300], "weight": 0.7})
        if len(out) >= 8:
            break
    return out


def regenerate_messages(detail):
    neighbours = [f"- {n.get('subject')!s}" for n in (detail["out_edges"] + detail["in_edges"])[:8]]
    prompt = (
        "Перепиши текст записи памяти так, чтобы он был ясным и понятным без контекста, "
        "одной-двумя фразами. Сохрани смысл. Новых фактов не добавляй. "
        "Если текст уже ясен, верни его без изменений.\n"
        f"Текст: «{detail['subject']}»\n"
        "Связанные записи (только для контекста):\n" + ("\n".join(neighbours) or "- нет") + "\n\n"
        "Ответь ТОЛЬКО JSON без пояснений и без markdown:\n"
        '{"subject": "новый текст", "confidence": null или число от 0 до 1, "reason": "что исправлено"}'
    )
    return [
        {"role": "system", "content": "Ты редактор памяти. Отвечаешь строго JSON."},
        {"role": "user", "content": prompt},
    ]


def sanitize_regen(p, old_subject):
    if not isinstance(p, dict):
        p = {}
    subj = p.get("subject")
    subj = subj.strip()[:1000] if isinstance(subj, str) and subj.strip() else None
    if subj == (old_subject or "").strip():
        subj = None
    conf = p.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and math.isfinite(conf):
        conf = max(0.0, min(1.0, float(conf)))
    else:
        conf = None
    return {"subject": subj, "confidence": conf, "reason": str(p.get("reason") or "")[:400]}


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


# ---------- автокоррекция всей памяти: ИИ предлагает, пользователь подтверждает ----------

AUTO_CANDIDATES = 30   # максимум записей в одном запросе к LLM (ответ должен уместиться в лимит токенов)
AUTO_ACTIONS = ("keep", "retract", "merge_into", "set_confidence", "add_link", "rewrite")


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def auto_candidates(store, limit=AUTO_CANDIDATES):
    """Кандидаты на автокоррекцию: шум (мало доверия / давно не использовались), повторы и изолированные.

    Возвращает список кратких карточек {id, subject, confidence, importance, degree}.
    """
    objs = store._objects
    now = datetime.now(timezone.utc)
    degree = defaultdict(int)
    for src, lst in store._graph._edges.items():
        for _rel, tgt in lst:
            if src in objs and tgt in objs:
                degree[src] += 1
                degree[tgt] += 1

    groups = defaultdict(list)
    for oid, o in objs.items():
        key = _norm_text(o.subject)
        if key:
            groups[key].append(oid)

    picked, seen_groups = [], set()
    ranked = sorted(objs.keys(), key=lambda k: (_conf(objs[k]), -degree[k]))
    # 1. сначала подозрительное: низкое доверие или застарелое
    for oid in ranked:
        o = objs[oid]
        meta = o.object if isinstance(o.object, dict) else {}
        base = _utc(o.created)
        last = _parse_dt(meta.get("last_accessed")) or base
        stale = base is not None and _conf(o) < STALE_CONFIDENCE and (now - last).days >= STALE_DAYS
        if _conf(o) >= LOW_CONFIDENCE and not stale:
            continue
        dup_of = groups.get(_norm_text(o.subject)) or []
        gkey = tuple(sorted(dup_of)) if len(dup_of) > 1 else None
        if gkey and gkey in seen_groups:
            continue
        if gkey:
            seen_groups.add(gkey)
        picked.append({"id": oid, "subject": str(o.subject or "")[:200],
                       "confidence": round(_conf(o), 3), "importance": round(_importance(o), 3),
                       "degree": degree.get(oid, 0)})
        if len(picked) >= limit:
            return picked
    # 2. группы повторов (по одному представителю группы сюда не берём — их видно в dups)
    # 3. изолированные уверенные: их стоит связать
    for oid in ranked:
        o = objs[oid]
        if degree.get(oid, 0) or _conf(o) < ISOLATED_CONFIDENCE:
            continue
        picked.append({"id": oid, "subject": str(o.subject or "")[:200],
                       "confidence": round(_conf(o), 3), "importance": round(_importance(o), 3),
                       "degree": 0})
        if len(picked) >= limit:
            break
    return picked


def auto_dups(store):
    """Группы текстовых повторов: [id1, id2, ...] — кандидаты на слияние (без LLM)."""
    groups = defaultdict(list)
    for oid, o in store._objects.items():
        key = _norm_text(o.subject)
        if key:
            groups[key].append(oid)
    return [g for g in groups.values() if len(g) > 1]


def auto_suggest_links(store, candidates_ids, threshold=0.82, max_links=15):
    """Эвристические предложения связей для изолированных записей по косинусу эмбеддингов.

    Без LLM. Матрица эмбеддингов нормируется один раз, а для каждой изолированной записи
    сходство считается одним матричным умножением (раньше — перебор всех пар в python).
    Каждое предложение применяется только после подтверждения пользователем.
    Возвращает [{source, target, relation:"RELATED_TO", weight, similarity}].
    """
    emb = store._embedding_index
    objs = store._objects
    if not emb:
        return []
    ids = [i for i in objs if i in emb]
    if len(ids) < 2:
        return []
    dim = len(emb[ids[0]])
    ids = [i for i in ids if len(emb[i]) == dim]
    pos = {i: k for k, i in enumerate(ids)}
    mat = np.asarray([emb[i] for i in ids], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0.0] = 1.0
    mat = mat / norms[:, None]

    def has_edges(node):
        return bool(store._graph.get_neighbors(node)) or bool(store._graph.get_incoming(node))

    out = []
    for cid in candidates_ids:
        if cid not in pos or has_edges(cid):
            continue  # связываем только полностью изолированные записи
        k = pos[cid]
        sims = mat @ mat[k]
        sims[k] = -1.0  # сама с собой не связываем
        best = int(np.argmax(sims))
        best_sim = float(sims[best])
        if best_sim > threshold:
            out.append({"source": cid, "target": ids[best], "relation": "RELATED_TO",
                        "weight": round(best_sim, 3), "similarity": round(best_sim, 3)})
            if len(out) >= max_links:
                break
    return out


def auto_scan_messages(candidates):
    """Промпт пакетной проверки: модель отвечает списком действий строго по id из кандидатов."""
    lines = [f"- id={c['id']} | доверие {c['confidence']} | связей {c['degree']} | «{c['subject']}»"
             for c in candidates]
    prompt = (
        "Это фрагмент долговременной памяти ассистента. Найди шум и мусор: записи неверные, "
        "устаревшие, бессмысленные или дублирующие другие. Оценивай важность, а не длину текста.\n"
        "Записи:\n" + "\n".join(lines) + "\n\n"
        "Для каждой проблемной записи предложи одно действие. Возможные действия:\n"
        '  {"id": "...", "action": "keep"} — запись важна, трогать не нужно (для keep пиши только если сомневался);\n'
        '  {"id": "...", "action": "retract", "reason": "кратко"} — удалить как шум/устаревшее;\n'
        '  {"id": "...", "action": "merge_into", "target_id": "...", "reason": "кратко"} — эта запись дубликат другой, слить в неё;\n'
        '  {"id": "...", "action": "set_confidence", "value": 0.0-1.0, "reason": "кратко"} — изменить доверие;\n'
        '  {"id": "...", "action": "add_link", "target_id": "...", "relation": "RELATED_TO|GROUNDS_IN|CAUSES|ENABLES|REQUIRES|PART_OF", "reason": "кратко"} — добавить связь;\n'
        '  {"id": "...", "action": "rewrite", "subject": "новый текст", "reason": "кратко"} — переписать мутный текст.\n'
        "Используй ТОЛЬКО id из списка выше. relations — только из разрешённых. Не выдумывай записи.\n"
        "Ответь ТОЛЬКО JSON без пояснений и без markdown:\n"
        '{"actions": [ ... ]}'
    )
    return [
        {"role": "system", "content": "Ты аудитор памяти. Отвечаешь строго JSON со списком actions."},
        {"role": "user", "content": prompt},
    ]


def sanitize_auto_actions(p, candidates):
    """Белый список для пакетного ответа: пропускаем только известные id и безопасные поля.

    Ничего не применяем — это только предложения, которые пользователь подтверждает по одному.
    """
    if not isinstance(p, dict):
        p = {}
    valid_ids = {c["id"] for c in candidates}
    raw = p.get("actions")
    if not isinstance(raw, list):
        raw = []
    out, seen = [], set()
    for a in raw[:60]:
        if not isinstance(a, dict):
            continue
        aid = a.get("id")
        action = a.get("action")
        if not isinstance(aid, str) or aid not in valid_ids:
            continue
        if action not in AUTO_ACTIONS:
            continue
        item = {"id": aid, "action": action, "reason": str(a.get("reason") or "")[:400]}
        if action == "keep":
            continue  # «всё хорошо» показывать не нужно
        tgt = a.get("target_id")
        if action in ("merge_into", "add_link"):
            if not isinstance(tgt, str) or tgt not in valid_ids or tgt == aid:
                continue
            item["target_id"] = tgt
        if action == "add_link":
            r = str(a.get("relation") or "").strip().upper()
            if r not in RELATIONS_ALLOWED or r in CONTRADICT_RELATIONS:
                r = "RELATED_TO"
            item["relation"] = r
        if action == "set_confidence":
            v = a.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                item["value"] = max(0.0, min(1.0, float(v)))
            else:
                continue
        if action == "rewrite":
            s = a.get("subject")
            if isinstance(s, str) and s.strip():
                item["subject"] = s.strip()[:1000]
            else:
                continue
        key = (aid, action, item.get("target_id"), item.get("relation"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
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
        "insights": cur.get("insights", []),
        "metrics": cur["metrics"],
    }