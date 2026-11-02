#!/usr/bin/env python3
"""
Миграция gcn_state.json к правилам доверия (GCN/memory_trust.py).

Что делает:
  * реплики пользователя и ассистента получают источник (source) и исходное доверие (base_confidence);
    реплики ассистента больше не считаются достоверными (1.0 -> 0.35);
  * остальные записи получают base_confidence, равный их текущему confidence
    (если в прошлом было накопление бонусов из-за дрейфа, оно останется — это видно в отчёте);
  * доверие пересчитывается из базы, проверок (metadata.verdicts) и числа противоречий;
  * перед записью создаётся резервная копия gcn_state.before_trust_migration.json;
  * запись атомарная (временный файл + os.replace).

Использование (сначала остановите приложение, иначе оно может перезаписать файл):
  python3 tools/migrate_memory_trust.py --user U2 --dry-run    # отчёт по памяти пользователя U2
  python3 tools/migrate_memory_trust.py --user U2              # миграция пользователя U2
  python3 tools/migrate_memory_trust.py --state путь/к/gcn_state.json   # произвольный файл

Память пользователя лежит в <корень>/<user_id>/cognitive_memory/gcn_state.json,
корень по умолчанию — папка ai_memory_v3 рядом с проектом (--root).

Все пользователи сразу (корень данных, внутри каждый <user_id>/cognitive_memory/gcn_state.json,
а также shared/ и global/, если они есть):
  python3 tools/migrate_memory_trust.py --root путь/к/данным --dry-run
  python3 tools/migrate_memory_trust.py --root путь/к/данным

  python tools\migrate_memory_trust.py --root ai_memory_v3 --dry-run
  python tools\migrate_memory_trust.py --root ai_memory_v3

  python tools\migrate_memory_trust.py --root ai_memory_v3\u2 --dry-run
  python tools\migrate_memory_trust.py --root ai_memory_v3\u2


Скрипт идемпотентен: повторный запуск не меняет записи, у которых уже есть base_confidence,
а доверие пересчитывает одинаково.
"""
import argparse
import collections
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEMORY_ROOT = ROOT / "ai_memory_v3"
sys.path.insert(0, str(ROOT))
from GCN import memory_trust as mt  # noqa: E402

EPISODE_SOURCES = {"user_message": mt.SOURCE_USER, "assistant_message": mt.SOURCE_ASSISTANT}


def _meta(obj):
    """Метаданные объекта как словарь (в старых файлах могла быть строка)."""
    m = obj.get("object")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = {}
    if not isinstance(m, dict):
        m = {}
    obj["object"] = m
    return m


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def migrate(data):
    """Меняет data на месте. Возвращает (статистика, распределение до, распределение после)."""
    objects = data.get("objects", {})
    edges = data.get("graph_edges", {})
    stats = collections.Counter()
    hist_before, hist_after = collections.Counter(), collections.Counter()

    for oid, o in objects.items():
        meta = _meta(o)
        old = _as_float(o.get("confidence"), 0.5)
        hist_before[round(old, 1)] += 1

        if "base_confidence" in meta:
            base = _as_float(meta["base_confidence"], old)
            stats["already_migrated"] += 1
        else:
            source = meta.get("source") or EPISODE_SOURCES.get(o.get("predicate"))
            base = mt.base_confidence(source, old)
            meta["base_confidence"] = base
            if source and not meta.get("source"):
                meta["source"] = source
            stats["base_set"] += 1
            if source == mt.SOURCE_ASSISTANT:
                stats["assistant_utterances"] += 1
            elif source == mt.SOURCE_USER:
                stats["user_utterances"] += 1
            else:
                stats["other_records"] += 1

        statuses = list(meta.get("verdicts") or [])
        contradictions = sum(1 for rel, tgt in edges.get(oid, [])
                             if rel == "CONTRADICTS" and tgt in objects)
        new = mt.trust(base, statuses, contradictions)
        if abs(new - old) > 1e-9:
            stats["confidence_changed"] += 1
        o["confidence"] = round(new, 4)
        hist_after[round(new, 1)] += 1

    return stats, hist_before, hist_after


def _report(stats, before, after):
    print("Итог миграции:")
    for key in ("base_set", "already_migrated", "assistant_utterances", "user_utterances",
                "other_records", "confidence_changed"):
        print(f"  {key:22s} {stats.get(key, 0)}")
    print("Распределение доверия (округлено до 0.1):")
    keys = sorted(set(before) | set(after))
    print("  значение  до    после")
    for k in keys:
        print(f"  {k:8.1f}  {before.get(k, 0):<6d}  {after.get(k, 0)}")


def migrate_file(path, dry_run):
    """Мигрирует один gcn_state.json. Возвращает True, если обработан."""
    print(f"\n=== {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    stats, before, after = migrate(data)
    _report(stats, before, after)
    if dry_run:
        print("dry-run: файл не изменён.")
        return True
    backup = path.with_name("gcn_state.before_trust_migration.json")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"резервная копия: {backup}")
    else:
        print(f"резервная копия уже есть, не перезаписываю: {backup}")
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)
    print(f"готово: {path}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--state", help="путь к одному gcn_state.json")
    group.add_argument("--root", help="корень данных: мигрирует все gcn_state.json внутри")
    ap.add_argument("--dry-run", action="store_true", help="только отчёт, файлы не меняются")
    args = ap.parse_args()

    if args.state:
        files = [Path(args.state)]
    else:
        root = Path(args.root)
        if not root.is_dir():
            sys.exit(f"папка не найдена: {root}")
        files = sorted(root.glob("**/cognitive_memory/gcn_state.json"))
        if not files:
            sys.exit(f"в {root} не найдено ни одного cognitive_memory/gcn_state.json")
        print(f"найдено файлов памяти: {len(files)}")

    for path in files:
        if not path.exists():
            sys.exit(f"файл не найден: {path}")
        migrate_file(path, args.dry_run)
    print(f"\nобработано файлов: {len(files)}" + (" (dry-run)" if args.dry_run else ""))




if __name__ == "__main__":
    main()