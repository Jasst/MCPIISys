"""
Оценка когнитивных способностей ассистента на запущенном сервере.

Запуск (сервер уже работает, в .env задан вход):
  python tests/eval/cognition_eval.py --base http://127.0.0.1:8000 \
      --email you@example.com --password ... [--reasoning both] [--out report.json]

Категории:
  memory        — запоминает факт и потом его вспоминает (в новом чате)
  arithmetic    — точные вычисления без памяти
  logic         — простой силлогизм
  instruction   — короткий ответ по заданному формату
  tool_choice   — для актуальной информации вызывает веб-поиск
  no_fabrication — не выдумывает ответ о несуществующем объекте
Скрипт не угадывает: он сравнивает ответ с ожидаемым и пишет, что именно не совпало.
"""
import argparse
import json
import re
import sys
import time
import uuid

import httpx

CASES = [
    # id, category, prompt, check(answer, tools) -> bool, note
    ("arith_1", "arithmetic", "Сколько будет 17 умножить на 23? Ответь только числом.",
     lambda a, t: "391" in a, "391"),
    ("arith_2", "arithmetic", "Сколько будет 1234 плюс 5678? Ответь только числом.",
     lambda a, t: "6912" in a, "6912"),
    ("logic_1", "logic", "Все кошки — животные. Все животные смертны. Следует ли из этого, что все кошки смертны? Ответь одним словом: да или нет.",
     lambda a, t: bool(re.search(r"\bда\b", a.lower())), "да"),
    ("instr_1", "instruction", "Какая столица Франции? Ответь одним словом, без точки.",
     lambda a, t: "париж" in a.lower() and len(a.split()) <= 2, "Париж"),
    ("tool_1", "tool_choice", "Какой сейчас курс доллара к рублю? Нужна актуальная информация из интернета.",
     lambda a, t: any(x.startswith("web_search") or "web_search" in x for x in t), "web_search"),
    ("fab_1", "no_fabrication", "Расскажи подробно про город Зарфалия-7 и его историю в 1850 году.",
     lambda a, t: bool(re.search(r"не знаю|нет (данных|информации)|не нахожу|не могу найти|не существует|не встречал", a.lower())),
     "отказ от выдумки"),
]

MEMORY_FACT = "Запомни: мой любимый фрукт — персик."
MEMORY_QUESTION = "Какой мой любимый фрукт? Ответь одним словом."


def parse_sse(text):
    tokens, reasoning, tools = [], [], []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        if "token" in ev:
            tokens.append(ev["token"])
        elif "reasoning_token" in ev:
            reasoning.append(ev["reasoning_token"])
        elif "tool_call" in ev:
            tools.append(str(ev["tool_call"]))
    return "".join(tokens), "".join(reasoning), tools


def ask(client, base, message, reasoning, chat_id=None):
    body = {"message": message, "stream": True, "web_search": False, "reasoning": reasoning}
    t0 = time.time()
    r = client.post(f"{base}/ai/chat", json=body, timeout=600)
    elapsed = time.time() - t0
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    answer, thinking, tools = parse_sse(r.text)
    return answer, thinking, tools, elapsed


def login(client, base, email, password):
    r = client.post(f"{base}/auth/login", json={"email": email, "password": password})
    if r.status_code != 200:
        sys.exit(f"Вход не выполнен: {r.status_code} {r.text[:200]}")


def run(base, email, password, modes):
    report = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "modes": {}}
    with httpx.Client(follow_redirects=False) as client:
        login(client, base, email, password)
        for reasoning in modes:
            key = "reasoning_on" if reasoning else "reasoning_off"
            rows = []
            # память: факт и вопрос в отдельном запросе; вопрос проверяем на «персик»
            ask(client, base, MEMORY_FACT, reasoning)
            a, _, t, el = ask(client, base, MEMORY_QUESTION, reasoning)
            rows.append({"id": "memory_1", "category": "memory", "ok": "персик" in a.lower(),
                         "expected": "персик", "answer": a[:200], "tools": t, "seconds": round(el, 1)})
            for cid, cat, prompt, check, expected in CASES:
                a, _, t, el = ask(client, base, prompt, reasoning)
                rows.append({"id": cid, "category": cat, "ok": bool(check(a, t)),
                             "expected": expected, "answer": a[:200], "tools": t, "seconds": round(el, 1)})
            report["modes"][key] = rows
    return report


def summarize(report):
    lines = []
    for mode, rows in report["modes"].items():
        lines.append(f"\n== {mode} ==")
        by_cat = {}
        for r in rows:
            by_cat.setdefault(r["category"], []).append(r["ok"])
        for cat, oks in by_cat.items():
            lines.append(f"  {cat:16} {sum(oks)}/{len(oks)}")
        total = sum(r["ok"] for r in rows)
        avg = sum(r["seconds"] for r in rows) / len(rows)
        lines.append(f"  {'TOTAL':16} {total}/{len(rows)}   средняя задержка {avg:.1f} с")
        for r in rows:
            if not r["ok"]:
                lines.append(f"  ✗ {r['id']}: ожидалось «{r['expected']}», ответ: {r['answer'][:120]!r}")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--reasoning", choices=["on", "off", "both"], default="both")
    ap.add_argument("--out", default="cognition_report.json")
    a = ap.parse_args()
    modes = {"on": [True], "off": [False], "both": [False, True]}[a.reasoning]
    rep = run(a.base, a.email, a.password, modes)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print(summarize(rep))
    print(f"\nПолный отчёт: {a.out}")
