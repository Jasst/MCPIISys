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


def edge_review_messages(store, source, relation, target):
    a = str(store._objects[source].subject or "")[:500]
    b = str(store._objects[target].subject or "")[:500]
    prompt = (
        "Проверь одну связь в долговременной памяти ассистента.\n"
        f"Запись A: «{a}»\n"
        f"Связь: {relation}\n"
        f"Запись B: «{b}»\n\n"
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