"""Защита аргументов модели и связанные правки: память, картинка, отложенное сохранение."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from GCN import command_parse as cmd  # noqa: E402
from GCN import internal_tools as it  # noqa: E402
from GCN.tool_router import ToolRegistry  # noqa: E402


# ---------- разбор ----------

def test_clean_fact_takes_only_command_content():
    assert cmd.clean_fact("запиши в память и сгенерируй картинку") is None
    assert cmd.clean_fact("запиши в память: Нел работает над BiChat") == "Нел работает над BiChat"


def test_clean_fact_keeps_ordinary_sentence_with_verb_inside():
    # слово «сохранила» внутри обычного факта — не команда
    assert cmd.clean_fact("Нел сохранила файл в архив") == "Нел сохранила файл в архив"


def test_clean_image_prompt_rejects_generic_and_command_only():
    assert cmd.clean_image_prompt("картинку") is None
    assert cmd.clean_image_prompt("сгенерируй картинку") is None
    assert cmd.clean_image_prompt("сгенерируй картинку кота в шляпе") == "кота в шляпе"
    assert cmd.clean_image_prompt("новость об ИИ") == "новость об ИИ"


def test_image_prompt_origin_topic_for_user_case():
    prompt, origin = cmd.image_prompt_origin(
        "найди новость об ИИ и попробуй визуализировать, объясни что ты визуализировал, "
        "запиши в память и сгенерируй картинку")
    assert (prompt, origin) == ("новость об ИИ", "topic")


def test_memory_text_from_answer_prefers_visualization_paragraphs():
    answer = ("<thought>внутреннее</thought>Новость: ИИ обновил модель.\n\n"
              "Я визуализировал это как картину: тёплые цвета и фигура у окна.\n\n"
              "Источник: example.com")
    text = cmd.memory_text_from_answer(answer)
    assert "картину" in text and "внутреннее" not in text and "Источник" not in text
    assert len(cmd.memory_text_from_answer("x" * 2000)) <= 500


# ---------- обработчики инструментов: защита от аргументов модели ----------

class _Mem:
    def __init__(self):
        self.calls = []

    async def remember(self, text, scope=None, user_explicit=False, **k):
        self.calls.append(text)
        return {"id": "x", "fact": text}


class _Ctl:
    def __init__(self):
        self.memory_service = _Mem()
        self.generated = []

    async def enhance_prompt(self, p):
        return p

    async def generate_image(self, prompt, **k):
        self.generated.append(prompt)
        return None


def _handler(register, ctl, name):
    reg = ToolRegistry()
    register(reg, ctl)
    return reg.get(name).handler


def test_remember_handler_rejects_command_only_fact():
    ctl = _Ctl()
    h = _handler(it._register_memory, ctl, "internal__remember")
    out = asyncio.run(h({"fact": "запиши в память и сгенерируй картинку"}))
    assert ctl.memory_service.calls == []
    assert "Не сохранено" in out


def test_remember_handler_strips_command_prefix():
    ctl = _Ctl()
    h = _handler(it._register_memory, ctl, "internal__remember")
    asyncio.run(h({"fact": "запомни: мой любимый цвет синий"}))
    assert ctl.memory_service.calls == ["мой любимый цвет синий"]


def test_image_handler_refuses_generic_prompt():
    ctl = _Ctl()
    h = _handler(it._register_image, ctl, "internal__generate_image")
    out = asyncio.run(h({"prompt": "картинку", "enhance_prompt": False}))
    assert ctl.generated == []
    assert "не создана" in out


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
