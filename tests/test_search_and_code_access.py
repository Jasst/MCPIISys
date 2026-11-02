"""
Тесты для изменений: уточнение поиска (query_refiner), реранкер вне event loop,
доступ к коду (code_analyzer: чтение по частям, запреты, кэш, поиск).

Запуск из папки проекта:  python -m pytest tests/test_search_and_code_access.py -q
Сеть и LLM не используются: поиск и LLM подменяются заглушками.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RERANK_ENABLED", "false")

import GCN.web_search as ws  # noqa: E402
from GCN import code_analyzer as ca  # noqa: E402


# ---------------------------------------------------------------- уточнение запроса

@pytest.mark.parametrize("orig,cand,expect", [
    ("курс доллара", "курс доллара ЦБ РФ сегодня", True),
    ("курс доллара", "курса доллара к рублю", True),        # словоформы допустимы
    ("курс доллара", "погода в москве", False),              # дрейф темы
    ("курс доллара", "", False),
    ("курс доллара", "курс доллара https://evil.example/x", False),
    ("курс доллара", "курс доллара", False),                 # без изменений
    ("курс доллара", "«курс доллара ЦБ»", True),             # кавычки срезаются
    ("курс доллара", "x" * 200, False),                      # слишком длинно
])
def test_sanitize_refined_query(orig, cand, expect):
    assert bool(ws.sanitize_refined_query(orig, cand)) == expect


def _fake_env(monkeypatch, second_score):
    calls = {"queries": [], "pp": 0}

    async def fake_backends(q, n):
        calls["queries"].append(q)
        return [{"title": f"r {q}", "url": f"https://ex.com/{abs(hash(q))}", "snippet": q}], "fake"

    async def fake_fetch(urls, limit=None, deadline=None):
        return [(u, "", []) for u in urls]

    async def fake_pp(query, fetched, *a, **k):
        calls["pp"] += 1
        score = 0.10 if calls["pp"] == 1 else second_score
        return ([{"title": "s", "url": f"https://ex.com/s{calls['pp']}"}],
                [f"part{calls['pp']}"], score, None)

    monkeypatch.setattr(ws, "search_backends", fake_backends)
    monkeypatch.setattr(ws._fetcher, "fetch_many_with_links", fake_fetch)
    monkeypatch.setattr(ws, "_process_pages", fake_pp)
    return calls


def _run(query, refiner, **kw):
    return asyncio.run(ws.deep_search(query, max_results=3, query_refiner=refiner,
                                      time_budget=60, **kw))


def test_refiner_accepted_when_score_improves(monkeypatch):
    calls = _fake_env(monkeypatch, second_score=0.5)

    async def good(q, titles):
        return "курс доллара ЦБ РФ"

    r = _run("курс доллара A", good)
    assert r["refinements"] == 1
    assert "курс доллара ЦБ РФ" in calls["queries"]
    assert r["chunks_found"] == 2


def test_refiner_drift_rejected(monkeypatch):
    calls = _fake_env(monkeypatch, second_score=0.5)

    async def drift(q, titles):
        return "погода в москве"

    _run("курс доллара B", drift)
    assert "погода в москве" not in calls["queries"]


def test_refiner_not_better_is_discarded(monkeypatch):
    _fake_env(monkeypatch, second_score=0.05)

    async def good(q, titles):
        return "курс доллара ЦБ РФ"

    r = _run("курс доллара C", good)
    assert r["chunks_found"] == 1      # результаты второго прохода отброшены


def test_refiner_exception_does_not_break_search(monkeypatch):
    calls = _fake_env(monkeypatch, second_score=0.5)

    async def boom(q, titles):
        raise RuntimeError("LM Studio down")

    # запрос без года, чтобы запасной вариант (добавить год) сработал
    r = _run("история ледникового периода", boom)
    assert r["search_performed"]
    assert calls["queries"][-1].endswith(str(time.localtime().tm_year))


@pytest.mark.parametrize("reply,expect", [
    ("курс доллара ЦБ РФ", "курс доллара ЦБ РФ"),
    ('["курс доллара ЦБ РФ", "other"]', "курс доллара ЦБ РФ"),
    ("  курс доллара ЦБ РФ\nпояснение модели", "курс доллара ЦБ РФ"),
    ("", ""),
])
def test_refiner_parser(reply, expect, monkeypatch):
    import json
    import logging
    import re
    from typing import List
    import types

    src = (ROOT / "routes" / "ai_assistant.py").read_text(encoding="utf-8")
    start = src.index("async def _search_query_refiner(")
    end = src.index("async def _search_query_expander(")

    async def fake_llm(messages, temp=0.0, max_tokens=0):
        return reply

    ns = {"json": json, "re": re, "List": List, "logger": logging.getLogger("t"),
          "call_llm": fake_llm}
    exec(src[start:end], ns)
    assert asyncio.run(ns["_search_query_refiner"]("курс доллара", ["Курс ЦБ"])) == expect


# ---------------------------------------------------------------- доступ к коду

@pytest.fixture
def project(tmp_path):
    for d in ("GCN", "routes", "static", "tests", "ai_memory_v3"):
        (tmp_path / d).mkdir()
    lines = [f"line_{i} = {i}" for i in range(1, 251)]
    (tmp_path / "GCN" / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "static" / "app.js").write_text("console.log(1);\n", encoding="utf-8")
    (tmp_path / "ai_memory_v3" / "history.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=1\n", encoding="utf-8")
    return tmp_path


def test_chunked_read_reassembles_file(project):
    an = ca.CodeAnalyzer(root_path=project)
    got, start, pages = [], 1, 0
    while start is not None:
        d = an.read_lines("GCN/big.py", start_line=start, max_lines=100)
        got += d["content"].split("\n")
        start = d["next_start_line"]
        pages += 1
    assert got == [f"line_{i} = {i}" for i in range(1, 251)]
    assert pages == 3 and d["total_lines"] == 250


@pytest.mark.parametrize("bad", [".env", "ai_memory_v3/history.json", "../etc/passwd",
                                 "/etc/passwd", "GCN/../.env"])
def test_forbidden_paths_blocked(project, bad):
    an = ca.CodeAnalyzer(root_path=project)
    with pytest.raises(ca.CodeAccessError) as e:
        an.read_lines(bad)
    assert e.value.code in ("forbidden", "not_found")


def test_symlink_out_of_project_blocked(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    (project / "GCN" / "leak.py").symlink_to(outside)
    an = ca.CodeAnalyzer(root_path=project)
    with pytest.raises(ca.CodeAccessError):
        an.read_lines("GCN/leak.py")


def test_cache_invalidated_after_edit(project):
    an = ca.CodeAnalyzer(root_path=project)
    assert an.read_lines("GCN/big.py", 1, 1)["content"] == "line_1 = 1"
    time.sleep(0.01)
    (project / "GCN" / "big.py").write_text("changed = 1\n", encoding="utf-8")
    assert an.read_lines("GCN/big.py", 1, 1)["content"] == "changed = 1"


def test_list_files_excludes_data_and_has_hashes(project):
    an = ca.CodeAnalyzer(root_path=project)
    paths = [f["file_path"] for f in an.list_files()["files"]]
    assert "static/app.js" in paths and "GCN/big.py" in paths
    assert not any("ai_memory" in p or p == ".env" for p in paths)
    assert all(f["sha256"] and f["lines"] > 0 for f in an.list_files()["files"])


def test_search_structured(project):
    an = ca.CodeAnalyzer(root_path=project)
    r = an.search_structured("line_42 ")
    assert r["results"] and r["results"][0]["file_path"] == "GCN/big.py"
    assert an.search_structured("[unclosed(")["results"] == []   # некорректный regex не падает
