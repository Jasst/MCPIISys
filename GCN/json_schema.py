"""Схемы структурированного вывода LLM и минимальный валидатор.

Схемы передаются бэкенду через response_format (OpenAI-совместимый json_schema, LM Studio).
Если бэкенд не понимает response_format, llm_client повторяет запрос без него, и
валидатор ниже становится единственной защитой от мусора в памяти.
"""
from typing import Any, Dict, Optional, Tuple

CONTRADICTION_VERDICT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "relation": {"type": "string",
                     "enum": ["true_contradiction", "false_positive", "both_partially_true"]},
        "keep": {"type": "string", "enum": ["A", "B", "both"]},
        "reason": {"type": "string"},
    },
    "required": ["relation", "keep", "reason"],
    "additionalProperties": False,
}

FACT_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}


def response_format(name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}


CONTRADICTION_RESPONSE_FORMAT = response_format("contradiction_verdict", CONTRADICTION_VERDICT_SCHEMA)
FACT_LIST_RESPONSE_FORMAT = response_format("fact_list", FACT_LIST_SCHEMA)

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def validate(value: Any, schema: Dict[str, Any], path: str = "$") -> Tuple[bool, Optional[str]]:
    """Проверяет подмножество JSON Schema: type, required, properties, items, enum, additionalProperties."""
    t = schema.get("type")
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"{path}: ожидалось число"
    elif t in _TYPES:
        if t == "boolean" and not isinstance(value, bool):
            return False, f"{path}: ожидалось boolean"
        if t != "boolean" and not isinstance(value, _TYPES[t]):
            return False, f"{path}: ожидался тип {t}"
    if "enum" in schema and value not in schema["enum"]:
        return False, f"{path}: значение {value!r} вне enum"
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return False, f"{path}: нет обязательного поля {key!r}"
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                return False, f"{path}: лишние поля {sorted(extra)}"
        for key, sub in props.items():
            if key in value:
                ok, err = validate(value[key], sub, f"{path}.{key}")
                if not ok:
                    return False, err
    if t == "array" and "items" in schema:
        for i, item in enumerate(value):
            ok, err = validate(item, schema["items"], f"{path}[{i}]")
            if not ok:
                return False, err
    return True, None
