"""command_parse.py — разбор составных команд пользователя.

Проблема, которую решает модуль. Детерминированные обработчики (память, картинка,
поиск) раньше брали текст «после глагола» регулярным выражением по всей фразе.
В фразе «найди новость об ИИ, визуализируй, объясни, запиши в память и сгенерируй
картинку» это давало мусор: в память уходило «и сгенерируй картинку», в картинку —
«картинку», в поиск — вся фраза целиком.

Идея. Находим все глаголы команд (память, картинка, визуализация, поиск, объяснение)
и каждой команде отдаём только текст до следующего глагола. Связки «и», «а», «затем»,
двоеточия и предлоги очищаем. Если содержимого у команды нет, она возвращает None,
и вызывающий код не выполняет её вслепую.

Модуль без зависимостей от модели и сети, поэтому тестируется отдельно.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

KIND_MEMORY = "memory"
KIND_IMAGE = "image"
KIND_VISUALIZE = "visualize"
KIND_SEARCH = "search"
KIND_EXPLAIN = "explain"

_VERBS: List[Tuple[str, "re.Pattern"]] = [
    (KIND_MEMORY, re.compile(
        r"(?:запомни\w*|сохрани\w*|запиши\w*)(?:\s+в\s+(?:личную\s+|общую\s+|глобальную\s+)?память)?",
        re.IGNORECASE)),
    (KIND_IMAGE, re.compile(
        r"(?:сгенерир\w*|нарису\w*|создай\s+(?:картинк\w*|изображени\w*|рисунок|фото)"
        r"|сделай\s+(?:картинк\w*|изображени\w*|рисунок|фото))",
        re.IGNORECASE)),
    # Только повелительное и инфинитив: «визуализируй», «визуализировать» — команды;
    # «визуализировал» в «объясни, что ты визуализировал» — описание прошлого, не команда.
    (KIND_VISUALIZE, re.compile(r"визуализир(?:уй\w*|овать|уйте)|проиллюстр(?:уй\w*|ировать)", re.IGNORECASE)),
    (KIND_SEARCH, re.compile(r"найди\w*|поищи\w*|поиск\w*|ищи\w*|загугли\w*", re.IGNORECASE)),
    (KIND_EXPLAIN, re.compile(r"объясни\w*|расскажи\w*|опиши\w*", re.IGNORECASE)),
]

# Связки и предлоги, которые стоят в начале фрагмента после глагола.
_LEAD_CONNECTIVES = re.compile(
    r"^(?:(?:и|а|также|затем|потом|после\s+этого|чтобы|что|чтоб|то)\s+|[,;:.!?\s-]+)+",
    re.IGNORECASE,
)
# Существительные картинки в начале фрагмента: «сгенерируй картинку кота» → «кота».
_PICTURE_NOUNS = re.compile(r"^(?:картинку|картинка|изображение|рисунок|фото|картинки)\s*", re.IGNORECASE)
_PREPOSITIONS = re.compile(r"^(?:с|про|о|об|обо|по|со|на)\s+", re.IGNORECASE)
# Хвосты, которые относятся к области памяти, а не к содержимому факта.
_SCOPE_TAILS = re.compile(
    r"\s+(?:в\s+(?:личную|общую|глобальную)\s+память|глобально|shared|global)\s*$", re.IGNORECASE)
# Вежливые хвосты: «пожалуйста», «плиз».
_POLITE_TAILS = re.compile(r"\s+(?:пожалуйста|плиз|побыстрее|срочно|заранее\s+спасибо)[!.?]*\s*$", re.IGNORECASE)


# Связки в конце фрагмента перед следующим глаголом: «новость об ИИ и попробуй» → «новость об ИИ».
_TRAIL_CONNECTIVES = re.compile(
    r"(?:[\s,;:]+(?:и|а|затем|потом|также|попробуй\w*|пожалуйста))+[\s,;:]*$", re.IGNORECASE)


def _clean(fragment: str) -> str:
    text = _LEAD_CONNECTIVES.sub("", fragment or "").strip()
    text = _TRAIL_CONNECTIVES.sub("", text).strip()
    text = text.strip(" \t\r\n,;:.!?-«»\"'")
    return text


def commands(message: str) -> List[Tuple[str, str]]:
    """Список (вид команды, очищенный текст её фрагмента) в порядке появления в фразе."""
    found: List[Tuple[int, int, str]] = []
    for kind, pattern in _VERBS:
        for m in pattern.finditer(message or ""):
            found.append((m.start(), m.end(), kind))
    found.sort(key=lambda x: x[0])
    # Перекрывающиеся совпадения (например, «визуализировать» и «ищи» внутри слова) не берём.
    kept: List[Tuple[int, int, str]] = []
    for item in found:
        if kept and item[0] < kept[-1][1]:
            continue
        kept.append(item)
    out: List[Tuple[str, str]] = []
    for i, (start, end, kind) in enumerate(kept):
        stop = kept[i + 1][0] if i + 1 < len(kept) else len(message)
        out.append((kind, _clean(message[end:stop])))
    return out


def kinds(message: str) -> List[str]:
    return [k for k, _ in commands(message)]


def is_compound(message: str) -> bool:
    """Больше одной команды разного вида в одной фразе."""
    return len(set(kinds(message))) >= 2


def _first_text(message: str, wanted: Tuple[str, ...]) -> Optional[str]:
    for kind, text in commands(message):
        if kind in wanted and text:
            return text
    return None


def memory_fact(message: str) -> Optional[str]:
    """Текст факта для «запиши/запомни/сохрани». None, если явного текста нет.

    «запиши в память и сгенерируй картинку» → None (сохранять нечего: текст
    объяснения появится только после ответа; см. отложенное сохранение).
    «запиши в память: Нел работает над BiChat» → «Нел работает над BiChat».
    """
    fact = _first_text(message, (KIND_MEMORY,))
    if not fact:
        return None
    fact = _SCOPE_TAILS.sub("", fact).strip(" ,.;:")
    return fact if len(fact) >= 5 else None


def wants_deferred_memory(message: str) -> bool:
    """Команда «запиши» есть, а текста к ней нет: сохранить ответ ассистента после ответа."""
    return any(k == KIND_MEMORY for k in kinds(message)) and memory_fact(message) is None


def search_query(message: str) -> Optional[str]:
    """Тема поиска из фрагмента команды поиска, без других команд.

    «найди новость об ИИ, визуализируй и объясни» → «новость об ИИ».
    None, если отдельной команды поиска нет (тогда вызывающий код берёт всю фразу).
    """
    text = _first_text(message, (KIND_SEARCH,))
    if not text:
        return None
    return text if len(text) >= 3 else None


def _polish_picture_text(text: str) -> str:
    t = _PICTURE_NOUNS.sub("", text or "").strip()
    t = _PREPOSITIONS.sub("", t).strip()
    t = _POLITE_TAILS.sub("", t).strip(" ,.;:!?")
    return t


def image_prompt_origin(message: str) -> Tuple[Optional[str], str]:
    """Промпт картинки и откуда он взят: 'explicit' | 'visualize' | 'topic' | ''."""
    for kind, text in commands(message):
        if kind == KIND_IMAGE:
            prompt = _polish_picture_text(text)
            if len(prompt) >= 3:
                return prompt, "explicit"
            break
    for kind, text in commands(message):
        if kind == KIND_VISUALIZE:
            prompt = _polish_picture_text(text)
            if len(prompt) >= 3:
                return prompt, "visualize"
            break
    topic = search_query(message)
    if topic and len(topic) >= 3:
        return topic, "topic"
    return None, ""


def image_prompt(message: str) -> Optional[str]:
    """Промпт для генерации картинки (см. image_prompt_origin)."""
    return image_prompt_origin(message)[0]


# ---------- защита аргументов, которые передаёт модель ----------
_STARTS_MEMORY_VERB = re.compile(r"^(?:запомни|сохрани|запиши)", re.IGNORECASE)
_STARTS_IMAGE_VERB = re.compile(r"^(?:сгенерир|нарису|визуализир|проиллюстр|создай|сделай)", re.IGNORECASE)
_SCOPE_TAIL_ONLY = re.compile(r"\s+(?:в\s+(?:личную|общую|глобальную)\s+память|глобально|shared|global)\s*$", re.IGNORECASE)
_GENERIC_PROMPTS = {"", "картинку", "картинка", "картинки", "изображение", "рисунок", "фото"}


def clean_fact(text: Optional[str]) -> Optional[str]:
    """Факт для remember из аргумента модели.

    Если текст начинается с команды «запиши/запомни/сохрани», берём только её содержимое.
    Иначе это обычный факт, и он сохраняется как есть. «Нел сохранила файл» не трогаем:
    слово внутри предложения командой не считается.
    """
    t = _LEAD_CONNECTIVES.sub("", (text or "").strip()).strip()
    if _STARTS_MEMORY_VERB.match(t):
        return memory_fact(t)
    t = _SCOPE_TAIL_ONLY.sub("", t).strip(" ,.;:")
    return t if len(t) >= 5 else None


def clean_image_prompt(text: Optional[str]) -> Optional[str]:
    """Промпт картинки из аргумента модели. None — генерировать нельзя (нет описания)."""
    t = _LEAD_CONNECTIVES.sub("", (text or "").strip()).strip()
    if _STARTS_IMAGE_VERB.match(t):
        p = image_prompt(t)
    else:
        p = _polish_picture_text(t)
    if not p:
        return None
    p = p.strip(" ,.;:!?")
    if len(p) < 3 or p.lower() in _GENERIC_PROMPTS:
        return None
    return p


# ---------- что сохранить из ответа по команде «запиши» без текста ----------
_VIS_WORDS = re.compile(r"визуализ|изображ|картин|иллюстр|рисун|фото", re.IGNORECASE)


def memory_text_from_answer(answer: str, max_chars: int = 500) -> str:
    """Абзацы ответа про визуализацию (если есть), иначе начало ответа. Без блока размышлений."""
    text = re.sub(r"<thought>.*?</thought>", "", answer or "", flags=re.S).strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    picked = [p for p in paras if _VIS_WORDS.search(p)]
    body = " ".join(picked) if picked else " ".join(paras)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:max_chars].rstrip()
