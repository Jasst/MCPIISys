"""Тесты разбора составных команд (память, картинка, визуализация, поиск)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from GCN import command_parse as cmd  # noqa: E402

USER_CASE = ("найди новость об ИИ и попробуй визуализировать, объясни что ты визуализировал, "
             "запиши в память и сгенерируй картинку")


def test_user_case_search_query_is_topic_not_whole_phrase():
    assert cmd.search_query(USER_CASE) == "новость об ИИ"


def test_user_case_memory_is_deferred_not_junk():
    # раньше в память уходило «и сгенерируй картинку»
    assert cmd.memory_fact(USER_CASE) is None
    assert cmd.wants_deferred_memory(USER_CASE) is True


def test_user_case_image_prompt_uses_topic_not_picture_word():
    # раньше промпт картинки был «картинку»
    assert cmd.image_prompt(USER_CASE) == "новость об ИИ"


def test_visualize_past_tense_is_not_a_command():
    kinds = cmd.kinds("объясни, что ты визуализировал")
    assert "visualize" not in kinds


def test_explicit_memory_text_is_kept():
    assert cmd.memory_fact("запиши в память: Нел работает над BiChat") == "Нел работает над BiChat"
    assert cmd.wants_deferred_memory("запиши в память: Нел работает над BiChat") is False


def test_memory_scope_tail_is_removed():
    assert cmd.memory_fact("запомни что кофе без сахара в общую память") == "кофе без сахара"


def test_explicit_image_description():
    assert cmd.image_prompt("сгенерируй картинку кота в шляпе, пожалуйста") == "кота в шляпе"


def test_visualize_with_description():
    assert cmd.image_prompt("визуализируй закат над морем") == "закат над морем"


def test_bare_image_command_without_topic_yields_none():
    # ни описания, ни темы поиска: картинку не генерируем вслепую
    assert cmd.image_prompt("сгенерируй картинку") is None


def test_plain_question_has_no_commands():
    assert cmd.kinds("какая погода в Амстердаме") == []
    assert cmd.is_compound("какая погода в Амстердаме") is False


def test_is_compound_detects_multiple_kinds():
    assert cmd.is_compound(USER_CASE) is True
    assert cmd.is_compound("сгенерируй картинку кота") is False


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
