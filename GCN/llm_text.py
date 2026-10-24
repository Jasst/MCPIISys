"""Чистые функции для текста ответов LLM: вырезание рассуждений и разбор гипотез.

Без сетевых зависимостей, поэтому их можно проверять отдельно от LM Studio.
"""
import re

THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
OPEN_THINK_RE = re.compile(r"<think>[\s\S]*$", re.IGNORECASE)
LIST_ITEM_RE = re.compile(r"^\s*(?:[-•*]|\d{1,2}[.)])\s+(.+?)\s*$")

# Типичные начала рассуждений, которые некоторые модели выводят прямо в content
REASONING_MARKERS = (
    "thinking process",
    "analyze the request",
    "constraint check",
    "draft 1",
    "wait, i need",
    "let me think",
)


def strip_thinking(text: str) -> str:
    """Убирает блоки <think>…</think>. Незакрытый <think> отбрасывает хвост целиком:
    всё после него — рассуждение, а не ответ. Если найден только закрывающий тег,
    всё до него считается рассуждением."""
    if not text:
        return ""
    cleaned = THINK_BLOCK_RE.sub("", text)
    cleaned = OPEN_THINK_RE.sub("", cleaned)
    if "</think>" in cleaned.lower():
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1]
    return cleaned.strip()


def looks_like_reasoning(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in REASONING_MARKERS)


def parse_hypotheses(text: str, limit: int = 3) -> list:
    """Берёт только пункты списка. Если текст похож на рассуждение, возвращает пустой список:
    лучше повторить запрос, чем превратить рассуждение модели в гипотезы и поисковые запросы."""
    clean = strip_thinking(text)
    if not clean or looks_like_reasoning(clean):
        return []
    out, seen = [], set()
    for line in clean.splitlines():
        m = LIST_ITEM_RE.match(line)
        if not m:
            continue
        h = m.group(1).replace("**", "").strip().strip("«»\"'").strip()
        if not (15 <= len(h) <= 240):
            continue
        if h.endswith("?") or looks_like_reasoning(h):
            continue
        key = h.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out
