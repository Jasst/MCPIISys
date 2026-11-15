"""
research_board.py — доска доказательств для агентного цикла.

Зачем нужна. Раньше результаты поиска жили в трассе инструментов и обрезались
до нескольких сотен символов на шаг. Модель видела только последний раунд и
отвечала «ватно». Доска решает три вещи:

  1. Закрепление: найденные источники, фрагменты страниц и проверенные выводы
     живут весь ход и не теряются между раундами.
  2. Нумерация: каждый источник получает id (S1, S2, ...), выводы — F1, F2, ...
     Модель и финальный ответ ссылаются на одни и те же id.
  3. Анализ: после каждого раунда инструментов аналитик-модель (текстовый вызов)
     обновляет выводы, пробелы и список источников, которые стоит открыть.

Модуль не зависит от сети и LLM: сетевые и языковые вызовы делают вызывающие
стороны. Это позволяет тестировать доску отдельно.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Бюджеты в символах. Модель локальная и с ограниченным контекстом, поэтому
# каждый вывод ограничен явно.
BOARD_PAGE_EXCERPT_CHARS = _env_int("BOARD_PAGE_EXCERPT_CHARS", 2400)   # фрагмент страницы на доске
BOARD_CANDIDATE_SNIPPET_CHARS = _env_int("BOARD_CANDIDATE_SNIPPET_CHARS", 220)
BOARD_TOOL_OUTPUT_CHARS = _env_int("BOARD_TOOL_OUTPUT_CHARS", 2800)      # то, что видит модель после инструмента
BOARD_RENDER_MAX_CHARS = _env_int("BOARD_RENDER_MAX_CHARS", 3200)        # доска в каждом раунде
BOARD_ANSWER_MAX_CHARS = _env_int("BOARD_ANSWER_MAX_CHARS", 9000)        # контекст для финального ответа
BOARD_OPEN_PER_CALL = _env_int("BOARD_OPEN_PER_CALL", 3)                 # страниц за один вызов open_page
ANALYST_ENABLED = _env_flag("AGENT_ANALYST_ENABLED", True)
ANALYST_MAX_TOKENS = _env_int("AGENT_ANALYST_MAX_TOKENS", 400)

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_SID_RE = re.compile(r"^S(\d+)$", re.IGNORECASE)
_FID_RE = re.compile(r"^F(\d+)$", re.IGNORECASE)
_CONTEXT_SPLIT = "\n\n---\n\n"


def normalize_url(url: str) -> str:
    """Ключ дедупликации: без пробелов, хвостового слэша и фрагмента."""
    u = (url or "").strip().rstrip(".,;")
    u = u.split("#", 1)[0]
    return u.rstrip("/")


def extract_message_urls(text: str) -> List[str]:
    return [normalize_url(u) for u in _URL_RE.findall(text or "")]


def split_context_by_url(context: str) -> Dict[str, str]:
    """Разбирает склеенный контекст deep_search на части по URL.

    Каждая часть формата «Источник: ...\\nURL: <url>\\n<текст>». Возвращает
    {нормализованный_url: текст_без_служебной_шапки}.
    """
    out: Dict[str, str] = {}
    if not context:
        return out
    for part in context.split(_CONTEXT_SPLIT):
        m = re.search(r"^URL:\s*(\S+)\s*$", part, re.MULTILINE)
        if not m:
            continue
        url = normalize_url(m.group(1))
        body_lines = part.splitlines()
        # убираем строки «Источник:» и «URL:» из начала текста
        body = "\n".join(
            ln for ln in body_lines
            if not ln.startswith("Источник:") and not ln.startswith("URL:")
        ).strip()
        if body:
            out[url] = body
    return out


@dataclass
class Source:
    sid: str
    url: str
    title: str = ""
    snippet: str = ""
    published: Optional[str] = None
    reliability: Optional[str] = None
    excerpt: str = ""           # лучший фрагмент страницы (если открыта или прочитана поиском)
    opened: bool = False        # текст страницы реально получен
    queries: List[str] = field(default_factory=list)

    def label(self) -> str:
        return f"[{self.sid}] {self.title or self.url}"


@dataclass
class Finding:
    fid: str
    claim: str
    sources: List[str]
    origin: str = "analyst"     # analyst | model


class EvidenceBoard:
    """Закреплённое состояние исследования на один пользовательский ход."""

    def __init__(self) -> None:
        self.question: str = ""
        self.user_urls: set = set()
        self.reset()

    # ----- жизненный цикл -----
    def reset(self, question: str = "") -> None:
        self.question = (question or "").strip()
        self.user_urls = set(extract_message_urls(self.question))
        self.sources: Dict[str, Source] = {}
        self._sid_by_url: Dict[str, str] = {}
        self.findings: List[Finding] = []
        self.gaps: List[str] = []
        self.suggested_open: List[str] = []
        self.memory_notes: List[str] = []     # результаты memory-инструментов за ход
        self._sid_seq = 0
        self._fid_seq = 0

    @property
    def is_empty(self) -> bool:
        return not self.sources and not self.findings and not self.memory_notes

    def add_memory(self, text: str, tool: str = "memory") -> None:
        """Закрепляет результат memory-инструмента (recall, memory_search, ...).
        Дубликаты пропускаются; хранится не больше 8 записей."""
        text = (text or "").strip()
        if not text or text.startswith(("Ничего не найдено", "Укажите запрос")):
            return
        entry = f"[{tool}] " + text[:1500]
        if entry not in self.memory_notes:
            self.memory_notes.append(entry)
        self.memory_notes = self.memory_notes[-8:]

    # ----- источники -----
    def add_source(self, url: str, title: str = "", snippet: str = "",
                   published: Optional[str] = None, reliability: Optional[str] = None,
                   query: Optional[str] = None) -> Source:
        key = normalize_url(url)
        sid = self._sid_by_url.get(key)
        if sid is None:
            self._sid_seq += 1
            sid = f"S{self._sid_seq}"
            src = Source(sid=sid, url=key, title=(title or "").strip(),
                         snippet=(snippet or "").strip(), published=published,
                         reliability=reliability)
            self.sources[sid] = src
            self._sid_by_url[key] = sid
        else:
            src = self.sources[sid]
            if title and not src.title:
                src.title = title.strip()
            if snippet and len(snippet) > len(src.snippet):
                src.snippet = snippet.strip()
            if published and not src.published:
                src.published = published
        if query and query not in src.queries:
            src.queries.append(query)
        return src

    def sid_for_url(self, url: str) -> Optional[str]:
        return self._sid_by_url.get(normalize_url(url))

    def set_excerpt(self, sid: str, text: str, opened: bool = True) -> None:
        src = self.sources.get(sid)
        if src is None or not text:
            return
        src.excerpt = text[:BOARD_PAGE_EXCERPT_CHARS]
        src.opened = src.opened or opened

    def unopened(self) -> List[Source]:
        return [s for s in self.sources.values() if not s.opened]

    def resolve(self, ref: str) -> Optional[Source]:
        """S3 или URL -> источник доски."""
        ref = (ref or "").strip()
        m = _SID_RE.match(ref)
        if m:
            return self.sources.get(f"S{int(m.group(1))}")
        sid = self.sid_for_url(ref)
        return self.sources.get(sid) if sid else None

    def is_allowed_url(self, url: str) -> bool:
        """Открывать можно только то, что встретилось в поиске или в сообщении пользователя."""
        key = normalize_url(url)
        return key in self._sid_by_url or key in self.user_urls

    def ingest_search_entry(self, entry: Dict[str, Any], query: Optional[str] = None) -> None:
        """Переносит запись старого формата {queries, sources, context} на доску."""
        q = query or ", ".join(entry.get("queries") or []) or None
        excerpts = split_context_by_url(entry.get("context", ""))
        for s in entry.get("sources") or []:
            url = s.get("url") or ""
            if not url:
                continue
            src = self.add_source(url, s.get("title", ""), s.get("snippet", "") or "",
                                  s.get("published"), s.get("reliability"), query=q)
            text = excerpts.get(normalize_url(url))
            if text:
                self.set_excerpt(src.sid, text, opened=True)

    # ----- выводы -----
    def add_finding(self, claim: str, source_ids: Sequence[str], origin: str = "analyst") -> Optional[Finding]:
        claim = (claim or "").strip()
        if not claim:
            return None
        key = claim.lower()[:80]
        for f in self.findings:
            if f.claim.lower()[:80] == key:
                # уточнили источники существующего вывода
                for sid in source_ids:
                    if sid not in f.sources:
                        f.sources.append(sid)
                return f
        self._fid_seq += 1
        f = Finding(fid=f"F{self._fid_seq}", claim=claim[:400],
                    sources=[s for s in source_ids if s], origin=origin)
        self.findings.append(f)
        return f

    def valid_sids(self, refs: Sequence[str]) -> List[str]:
        """Оставляет только существующие S# и нестандартные метки (например, «memory»).
        Несуществующие S# и F# отбрасываются: модель могла их выдумать."""
        out: List[str] = []
        for raw in refs or []:
            ref = str(raw).strip()
            m = _SID_RE.match(ref)
            if m:
                sid = f"S{int(m.group(1))}"
                if sid in self.sources and sid not in out:
                    out.append(sid)
            elif ref and not _FID_RE.match(ref) and ref[:60] not in out:
                out.append(ref[:60])
        return out

    def apply_analysis(self, analysis: Dict[str, Any]) -> None:
        for item in analysis.get("findings", []):
            self.add_finding(item.get("claim", ""), self.valid_sids(item.get("sources", [])), origin="analyst")
        self.gaps = [g[:200] for g in analysis.get("gaps", [])][:5]
        self.suggested_open = [sid for sid in self.valid_sids(analysis.get("open", []))
                               if sid in self.sources and not self.sources[sid].opened][:BOARD_OPEN_PER_CALL]

    # ----- рендер -----
    def _source_line(self, s: Source, snippet_chars: int) -> str:
        meta = []
        if s.published:
            meta.append(s.published)
        if s.opened:
            meta.append("открыта")
        head = f"[{s.sid}] {s.title or s.url} — {s.url}"
        if meta:
            head += f" ({', '.join(meta)})"
        snippet = (s.snippet or "").replace("\n", " ")[:snippet_chars]
        return head + (f"\n    {snippet}" if snippet else "")

    def render_board(self, max_chars: int = BOARD_RENDER_MAX_CHARS) -> str:
        """Блок доски, который получает модель на каждом раунде (заменяет предыдущий)."""
        if self.is_empty and not self.gaps:
            return ""
        parts: List[str] = ["=== ДОСКА ИССЛЕДОВАНИЯ ==="]
        if self.findings:
            parts.append("Закреплённые выводы (не теряй их, опирайся на них):")
            for f in self.findings:
                refs = ",".join(f.sources) if f.sources else "без источника"
                parts.append(f"- [{f.fid}] {f.claim} (источники: {refs})")
        if self.memory_notes:
            parts.append("Из памяти (закреплено):")
            parts += [f"- {m[:300]}" for m in self.memory_notes[-3:]]
        if self.gaps:
            parts.append("Пробелы, которые ещё нужно закрыть:")
            parts += [f"- {g}" for g in self.gaps]
        if self.sources:
            parts.append("Источники:")
            parts += [self._source_line(s, 160) for s in self.sources.values()][:10]
        if self.suggested_open:
            parts.append("Стоит открыть целиком (open_page): " + ", ".join(self.suggested_open))
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n...[доска сокращена]"
        return text

    def render_candidates(self, sids: Optional[Sequence[str]] = None, excerpt_chars: int = 0,
                          max_chars: int = BOARD_TOOL_OUTPUT_CHARS) -> str:
        """Вывод инструмента для модели: список источников с id и, опционально, фрагментом."""
        chosen = [self.sources[s] for s in sids if s in self.sources] if sids else list(self.sources.values())
        lines: List[str] = []
        for s in chosen:
            lines.append(self._source_line(s, BOARD_CANDIDATE_SNIPPET_CHARS))
            if excerpt_chars and s.excerpt:
                lines.append("    фрагмент: " + s.excerpt[:excerpt_chars].replace("\n", " ") + " …")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n...[сокращено]"
        return text

    def render_for_answer(self, max_chars: int = BOARD_ANSWER_MAX_CHARS) -> str:
        """Контекст для финального ответа: выводы, затем тексты открытых источников,
        затем сниппеты неоткрытых. Метки [S#] совпадают с метками на доске."""
        if self.is_empty:
            return ""
        blocks: List[str] = []
        if self.memory_notes:
            blocks.append("Данные из памяти:\n" + "\n".join(self.memory_notes))
        if self.findings:
            blocks.append("Проверенные выводы:\n" + "\n".join(
                f"- {f.claim} [{', '.join(f.sources) or '—'}]" for f in self.findings))
        opened = [s for s in self.sources.values() if s.excerpt]
        for s in opened:
            head = f"[{s.sid}] {s.title or s.url}\nURL: {s.url}"
            if s.published:
                head += f"\nДата: {s.published}"
            blocks.append(head + "\n" + s.excerpt)
        for s in self.sources.values():
            if not s.excerpt and s.snippet:
                blocks.append(f"[{s.sid}] {s.title or s.url}\nURL: {s.url}\n[сниппет — страница не прочитана]\n"
                              + s.snippet[:600])
        out = _CONTEXT_SPLIT.join(blocks)
        if len(out) > max_chars:
            out = out[:max_chars].rstrip() + "\n...[контекст сокращён]"
        return out

    def as_search_entry(self) -> Dict[str, Any]:
        """Запись формата _web_search_results_this_turn: сохраняет совместимость
        с существующей логикой финального ответа (источники [N] и контекст)."""
        ordered = sorted(self.sources.values(), key=lambda s: int(s.sid[1:]))
        return {
            "queries": sorted({q for s in ordered for q in s.queries})[:10],
            "sources": [
                {k: v for k, v in {"url": s.url, "title": s.title or s.url,
                                   "reliability": s.reliability, "published": s.published}.items() if v}
                for s in ordered
            ],
            "context": self.render_for_answer(),
        }


# ----- аналитик -----
ANALYST_PROMPT = """Ты — аналитик исследования. Вопрос пользователя: {question}

Уже закреплённые выводы:
{findings}

Известные пробелы:
{gaps}

Результаты последнего шага инструментов:
{round_results}

Ответь ТОЛЬКО одним JSON-объектом без пояснений и без markdown:
{{"findings": [{{"claim": "короткий факт, подтверждённый результатами", "sources": ["S1"]}}],
  "gaps": ["что ещё неизвестно для ответа"],
  "open": ["S2"],
  "enough": false}}

Правила:
- findings только то, что явно следует из результатов шага; не добавляй знаний «от себя».
- open: до 3 идентификаторов источников, которые стоит прочитать целиком; только из списка результатов.
- enough: true, если вопрос можно полно и точно ответить уже сейчас."""


def build_analyst_prompt(board: EvidenceBoard, round_results_text: str) -> str:
    findings = "\n".join(f"- [{f.fid}] {f.claim}" for f in board.findings) or "(пока нет)"
    gaps = "\n".join(f"- {g}" for g in board.gaps) or "(не выявлены)"
    return ANALYST_PROMPT.format(
        question=board.question[:400] or "(не указан)",
        findings=findings,
        gaps=gaps,
        round_results=round_results_text[:2400] or "(нет)",
    )


def parse_analysis(raw: Any) -> Optional[Dict[str, Any]]:
    """Терпимый разбор ответа аналитика: допускает текст вокруг JSON.

    Возвращает нормализованный словарь или None, если JSON не найден.
    Модель может ошибиться; тогда цикл продолжается без обновления выводов.
    """
    if not raw or not isinstance(raw, str):
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    findings = []
    for item in data.get("findings") or []:
        if isinstance(item, dict) and isinstance(item.get("claim"), str):
            srcs = item.get("sources") or []
            if isinstance(srcs, str):
                srcs = [srcs]
            findings.append({"claim": item["claim"], "sources": [str(s) for s in srcs if isinstance(s, (str, int))]})

    def _strlist(key: str) -> List[str]:
        val = data.get(key) or []
        if isinstance(val, str):
            val = [val]
        return [str(v) for v in val if isinstance(v, (str, int))]

    return {
        "findings": findings,
        "gaps": _strlist("gaps"),
        "open": _strlist("open"),
        "enough": data.get("enough") is True,
    }
