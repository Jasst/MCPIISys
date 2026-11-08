"""Калибровка порогов идентичности по размеченным парам.

Вход (JSONL, одна строка на пример):
  {"kind": "handshake",   "a": "новая версия голоса", "b": "предыдущий голос", "same": true}
  {"kind": "consistency", "a": "ответ ассистента",    "b": "текст ТЕКУЩЕЕ_Я",   "same": false}
  same=true означает «в характере / корректное продолжение», false — «дрейф / эхо».

Использование (нужны sentence_transformers и модель из config_ai):
  python tools/calibrate_thresholds.py pairs.jsonl
Результат пишется в GCN/calibration.json и отчёт печатается в stdout.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from GCN.calibration import best_threshold, load_calibration, save_calibration  # noqa: E402

KEYS = {"handshake": "IDENTITY_HANDSHAKE_THRESHOLD", "consistency": "IDENTITY_CONSISTENCY_THRESHOLD"}
CAL_PATH = os.path.join(ROOT, "GCN", "calibration.json")


def _cosine(u, v) -> float:
    import numpy as np
    u, v = np.asarray(u, dtype=float), np.asarray(v, dtype=float)
    d = np.linalg.norm(u) * np.linalg.norm(v)
    return float(u @ v / d) if d else 0.0


def main(path: str) -> int:
    from sentence_transformers import SentenceTransformer
    from GCN.config_ai import EMBEDDING_MODEL, EMBEDDING_PASSAGE_PREFIX

    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    model = SentenceTransformer(EMBEDDING_MODEL)
    # тот же префикс, что в боевом коде для текстов памяти и ответов
    texts = sorted({r["a"] for r in rows} | {r["b"] for r in rows})
    vecs = dict(zip(texts, model.encode([EMBEDDING_PASSAGE_PREFIX + t for t in texts],
                                         normalize_embeddings=True)))
    result = load_calibration(CAL_PATH)
    for kind, key in KEYS.items():
        sub = [r for r in rows if r.get("kind") == kind]
        if not sub:
            print(f"[{kind}] примеров нет, порог не меняется")
            continue
        scores = [_cosine(vecs[r["a"]], vecs[r["b"]]) for r in sub]
        labels = [bool(r["same"]) for r in sub]
        thr, f1, p, r = best_threshold(scores, labels)
        result[key] = round(thr, 4)
        print(f"[{kind}] n={len(sub)} порог={thr:.4f} F1={f1:.3f} precision={p:.3f} recall={r:.3f}")
    save_calibration(CAL_PATH, result)
    print(f"сохранено: {CAL_PATH}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
