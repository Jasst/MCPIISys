"""Метрика качества выдачи памяти: hit@k и MRR на фиксированном наборе вопросов.

Вход (JSONL, одна строка на вопрос):
  {"query": "где я работаю", "relevant": ["работает в", "офис"]}
Результат релевантен, если его текст содержит ЛЮБУЮ подстроку из relevant
(без учёта регистра). Так набор не привязан к внутренним id и переживает
пересборку графа.

Использование (нужны зависимости проекта и данные пользователя):
  python tools/eval_retrieval.py USER_ID questions.jsonl --k 5 --out report.json

Сравнение вариантов: запустить дважды с разными переменными окружения
(например LATERAL_INHIBITION_ENABLED=0 и =1) и сравнить два отчёта.
Отчёт содержит hit@k, MRR и поимённо промахи, чтобы видеть, что именно сломалось.
"""
import argparse
import asyncio
import json
import os
import sys
from typing import Dict, Iterable, List, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def is_relevant(text: str, substrings: Iterable[str]) -> bool:
    low = (text or "").lower()
    return any(s and s.lower() in low for s in substrings)


def first_relevant_rank(ranked_texts: Sequence[str], substrings: Iterable[str], k: int) -> int:
    """Позиция (1-based) первого релевантного результата в top-k, 0 если нет."""
    subs = list(substrings)
    for pos, text in enumerate(ranked_texts[:k], start=1):
        if is_relevant(text, subs):
            return pos
    return 0


def hit_at_k(ranked_texts: Sequence[str], substrings: Iterable[str], k: int) -> bool:
    return first_relevant_rank(ranked_texts, substrings, k) > 0


def reciprocal_rank(ranked_texts: Sequence[str], substrings: Iterable[str], k: int) -> float:
    rank = first_relevant_rank(ranked_texts, substrings, k)
    return 1.0 / rank if rank else 0.0


def summarize(per_query: List[Dict]) -> Dict[str, float]:
    n = len(per_query)
    if n == 0:
        return {"n": 0, "hit_at_k": 0.0, "mrr": 0.0}
    return {
        "n": n,
        "hit_at_k": round(sum(1 for q in per_query if q["hit"]) / n, 4),
        "mrr": round(sum(q["rr"] for q in per_query) / n, 4),
    }


def load_questions(path: str) -> List[Dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("query") or not row.get("relevant"):
                raise ValueError(f"в строке нет query или relevant: {line[:120]}")
            rows.append(row)
    return rows


async def run(uid: str, rows: List[Dict], k: int) -> List[Dict]:
    from GCN.memory_graph import get_memory_service

    service = await get_memory_service(uid)
    memory = service.private_memory
    out = []
    for row in rows:
        results = await memory.recall(row["query"], top_k=k)
        texts = [r.get("text", "") for r in results]
        out.append({
            "query": row["query"],
            "hit": hit_at_k(texts, row["relevant"], k),
            "rr": reciprocal_rank(texts, row["relevant"], k),
            "top": texts[:k],
        })
    return out


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("user_id")
    ap.add_argument("questions")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    rows = load_questions(args.questions)
    per_query = asyncio.run(run(args.user_id, rows, args.k))
    report = {
        "k": args.k,
        "flags": {name: os.getenv(name, "") for name in (
            "LATERAL_INHIBITION_ENABLED", "LATERAL_INHIBITION_LAMBDA",
            "INHIBITORY_SYNAPSES_ENABLED", "STABILIZER_ENABLED")},
        "summary": summarize(per_query),
        "misses": [q for q in per_query if not q["hit"]],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
