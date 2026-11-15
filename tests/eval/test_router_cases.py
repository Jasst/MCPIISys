"""Оценка подсказок роутера на размеченных формулировках (без LLM, детерминированно).
Проверяет: нужен ли инструмент (might_need_tools) и попадает ли ожидаемый инструмент в топ-3
ранжирования. Порог качества задан в MIN_ACCURACY; падение ниже него — регрессия маршрутизации.
Запуск: python tests/eval/test_router_cases.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from GCN.tool_router import ToolRegistry, rank_tools, might_need_tools  # noqa: E402

CASES = Path(__file__).with_name("router_cases.jsonl")
MIN_ACCURACY = 0.9

TOOL_NAMES = {
    "recall": "Поиск в памяти", "remember": "Запоминает факты", "web_search": "Поиск в интернете",
    "memory_search": "Ищет факты в памяти и возвращает id", "memory_forget": "Удаляет факт из памяти",
    "memory_update": "Исправляет факт: заменяет его текст", "memory_explain": "Происхождение факта",
    "generate_image": "Генерирует изображение по описанию", "inspect_image": "Повторно рассматривает изображение",
    "project_structure": "Структура проекта в виде дерева", "read_code": "Читает файл исходного кода",
    "search_code": "Ищет текст в коде проекта", "analyze_error": "Анализирует ошибку по traceback",
    "get_identity_chain": "Цепочка ТЕКУЩЕЕ_Я идентичности", "contribute_to_identity": "Добавляет звено идентичности",
    "list_tools": "Список инструментов", "fetch_github_file": "Файл из GitHub", "add_goal": "Добавляет цель",
    "ask_user": "Задаёт уточняющий вопрос пользователю",
}


def _registry():
    reg = ToolRegistry()

    async def h(a):
        return "ok"
    for name, desc in TOOL_NAMES.items():
        reg.register(name, desc, {"type": "object", "properties": {}}, h)
    return reg


def evaluate():
    reg = _registry()
    specs = reg._visible()
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    need_ok, top3_ok, total_tool = 0, 0, 0
    misses = []
    for c in cases:
        need = might_need_tools(c["message"]) == c["needs_tool"]
        need_ok += need
        if c["expect"]:
            total_tool += 1
            ranked, _ = rank_tools(c["message"], specs)
            hit = c["expect"] in ranked[:3]
            top3_ok += hit
            if not hit:
                misses.append((c["message"], c["expect"], ranked[:3]))
        if not need:
            misses.append((c["message"], "needs_tool=False", might_need_tools(c["message"])))
    total = len(cases)
    return need_ok / total, (top3_ok / total_tool if total_tool else 1.0), misses


def test_router_eval():
    need_acc, top3_acc, misses = evaluate()
    print(f"маршрутизация (нужен ли инструмент): {need_acc:.0%}")
    print(f"ожидаемый инструмент в топ-3: {top3_acc:.0%}")
    for m in misses:
        print("  промах:", m)
    assert need_acc >= MIN_ACCURACY, need_acc
    assert top3_acc >= MIN_ACCURACY, top3_acc


if __name__ == "__main__":
    test_router_eval()
    print("ROUTER EVAL PASSED")
