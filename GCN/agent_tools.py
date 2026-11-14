"""agent_tools.py — инструменты самого агента (не данные и не внешние сервисы).

ask_user: вместо угадывания неоднозначного запроса агент задаёт один точный вопрос.
Успешный вызов завершает цикл роутера (stop_reason='ask_user'), вопрос уходит пользователю.
Используйте, только если без ответа действие нельзя выполнить безопасно.
"""
from .tool_router import ToolRegistry


def register_agent_tools(registry: ToolRegistry) -> None:
    async def _ask_user(args):
        q = (args.get("question") or "").strip()
        if len(q) < 3:
            return "Ошибка: укажи конкретный вопрос (question)."
        return q

    registry.register(
        name="ask_user",
        description=("Задаёт пользователю ОДИН уточняющий вопрос, когда запрос неоднозначен и "
                     "действие без ответа может навредить (например, удаление). Аргумент: question (str)."),
        parameters={"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        handler=_ask_user, server="internal", timeout_seconds=5)
