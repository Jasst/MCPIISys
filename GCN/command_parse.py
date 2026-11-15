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


def image_prompt(message: str) -> Optional[str]:
    """Промпт для генерации картинки.

    1) явное описание после команды картинки: «сгенерируй картинку кота в шляпе» → «кота в шляпе»;
    2) иначе описание после «визуализируй»: «визуализируй закат над морем» → «закат над морем»;
    3) иначе тема поиска: «найди новость об ИИ … сгенерируй картинку» → «новость об ИИ».
    None, если ни одного из этого нет. Тогда картинку не генерируем.
    """
    def _strip_picture(text: str) -> str:
        t = _PICTURE_NOUNS.sub("", text).strip()
        t = _PREPOSITIONS.sub("", t).strip()
        t = _POLITE_TAILS.sub("", t).strip(" ,.;:!?")
        return t

    for kind, text in commands(message):
        if kind == KIND_IMAGE:
            prompt = _strip_picture(text)
            if len(prompt) >= 3:
                return prompt
            break  # явная команда без описания: дальше смотрим визуализацию и тему поиска

    for kind, text in commands(message):
        if kind == KIND_VISUALIZE:
            prompt = _strip_picture(text)
            if len(prompt) >= 3:
                return prompt
            break

    topic = search_query(message)
    if topic and len(topic) >= 3:
        return topic
    return None
