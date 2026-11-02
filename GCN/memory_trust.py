"""
Правила доверия к записям памяти.

Модуль намеренно без тяжёлых зависимостей (только stdlib), чтобы правила
можно было тестировать отдельно от эмбеддингов и FAISS.

Принципы:
- доверие определяется источником и проверкой, а не количеством повторов;
- реплика ассистента — не факт, пока её не подтвердили;
- повтор уже известной записи — это счётчик, а не доказательство;
- опровергнутое утверждение не возвращается в контекст как факт;
- важность от обращений насыщается (логарифм), чтобы петля
  «извлекли → стало важнее → извлекут ещё» не разгонялась бесконечно.
"""
import math
import re

# источники записей
SOURCE_USER = "user_utterance"          # то, что сказал пользователь
SOURCE_ASSISTANT = "assistant_utterance"  # то, что ответил ассистент (не проверено)
SOURCE_FACT = "fact"                    # извлечённое или явно добавленное утверждение

# исходное доверие по источнику (до проверок)
USER_UTTERANCE_CONFIDENCE = 0.8
ASSISTANT_UTTERANCE_CONFIDENCE = 0.35

# статусы проверок (provenance.verifications[].status)
VERIFY_OK = "ok"              # подтверждено человеком или инструментом
VERIFY_DISPUTED = "disputed"  # под сомнением (например, пользователь поправил в чате)
VERIFY_WRONG = "wrong"        # опровергнуто явно

REFUTED_CONFIDENCE = 0.05
DISPUTED_PENALTY = 0.25
OK_BONUS_EACH = 0.1
OK_BONUS_MAX = 0.4
CONTRADICTION_PENALTY = 0.05

ACCESS_SATURATION = 50        # после стольких обращений важность почти не растёт
CONTEXT_TRUST_FLOOR = 0.0     # опровергнутое не попадает в контекст при любом значении

NEGATION_WORDS = frozenset({
    "не", "нет", "без", "против", "отрицает", "опровергает", "никогда", "ни",
    "not", "no", "never", "without",
})

_WORD = re.compile(r"\w+", re.UNICODE)


def has_negation(text):
    """Отрицание ищем по целым словам: подстрока «не» есть в «мне», «небо», «менеджер»."""
    return any(tok in NEGATION_WORDS for tok in _WORD.findall(str(text or "").lower()))


def access_importance(count):
    """Вклад обращений в важность: 0..1, логарифмически насыщается."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        return 0.0
    if n <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(ACCESS_SATURATION))


def base_confidence(source, fallback):
    """Исходное доверие записи по её источнику."""
    if source == SOURCE_ASSISTANT:
        return ASSISTANT_UTTERANCE_CONFIDENCE
    if source == SOURCE_USER:
        return USER_UTTERANCE_CONFIDENCE
    return fallback


def summarize_verifications(statuses):
    """(число подтверждений, опровергнуто ли, под сомнением ли) по списку статусов."""
    ok = sum(1 for s in statuses if s == VERIFY_OK)
    refuted = any(s == VERIFY_WRONG for s in statuses)
    disputed = any(s == VERIFY_DISPUTED for s in statuses)
    return ok, refuted, disputed


def trust(base, statuses, contradictions):
    """Итоговое доверие. Зависит от базы, проверок и противоречий, но НЕ от повторов."""
    ok, refuted, disputed = summarize_verifications(statuses)
    if refuted:
        return REFUTED_CONFIDENCE
    value = base + min(ok * OK_BONUS_EACH, OK_BONUS_MAX)
    if disputed:
        value -= DISPUTED_PENALTY
    value -= contradictions * CONTRADICTION_PENALTY
    return max(0.0, min(1.0, value))


def trust_label(source, statuses):
    """Короткая пометка для промпта: что это за утверждение и насколько ему верить."""
    ok, refuted, disputed = summarize_verifications(statuses)
    if refuted:
        return "опровергнуто"
    if disputed:
        return "под сомнением"
    if ok:
        return "подтверждено"
    if source == SOURCE_ASSISTANT:
        return "ответ ассистента, не проверено"
    if source == SOURCE_USER:
        return "со слов пользователя"
    return None


def is_refuted(statuses):
    return any(s == VERIFY_WRONG for s in statuses)


def select_similar(pairs, threshold, max_neighbours):
    """Выбирает соседей для синапса: только достаточно близкие и не больше max_neighbours."""
    chosen = []
    for oid, sim in sorted(pairs, key=lambda p: -p[1]):
        if sim < threshold:
            break
        chosen.append((oid, sim))
        if len(chosen) >= max_neighbours:
            break
    return chosen

# связи «похожести» между записями (косинус эмбеддингов).
# Порог 0.45 создавал почти полный граф. 0.8 — стартовое значение, его нужно откалибровать
# на реальных данных (распределение косинусов для multilingual-e5 зависит от языка и длины текста).
SYNAPSE_SIMILARITY_THRESHOLD = 0.8
SYNAPSE_SIMILAR_MAX = 5