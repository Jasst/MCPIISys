"""vision_tools.py — повторный осмотр изображения из текущего сообщения.

Зачем: одного прохода по всей картинке часто мало (мелкий текст, деталь в углу).
Инструмент вырезает область (доли 0..1) и задаёт модели точный вопрос только по ней.
Модель может вызывать его несколько раз подряд: обзор -> зум на деталь -> уточнение.

Работает только тогда, когда текущая модель принимает изображения: в чат картинка
попадает лишь при LM_STUDIO_VISION_SUPPORTED, иначе инструмент честно сообщает об этом.
"""
import asyncio
import base64
import io
import logging
from typing import Any, Dict, Optional, Sequence

from .tool_router import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_SIDE = 1600  # крупнее не нужно: модель всё равно уменьшает вход


def _decode_b64(data: str) -> bytes:
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    return base64.b64decode(data)


def crop_image_b64(image_b64: str, region: Optional[Sequence[float]]) -> str:
    """Возвращает PNG (base64) вырезанной области. region=None — всё изображение."""
    from PIL import Image

    img = Image.open(io.BytesIO(_decode_b64(image_b64)))
    img = img.convert("RGB")
    w, h = img.size
    if region and len(region) == 4:
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in region)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("region: нужны x0<x1 и y0<y1 (доли от 0 до 1)")
        img = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    if max(img.size) > _MAX_SIDE:
        scale = _MAX_SIDE / max(img.size)
        img = img.resize((max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def register_vision_tools(registry: ToolRegistry, controller, call_llm_fn) -> None:
    async def _inspect(args: Dict[str, Any]) -> str:
        current = controller.current_turn_image() if hasattr(controller, "current_turn_image") else None
        if not current:
            return "В этом сообщении нет изображения (или текущая модель не принимает изображения)."
        question = (args.get("question") or "Опиши, что видно на изображении.").strip()
        region = args.get("region")
        try:
            png_b64 = await asyncio.to_thread(crop_image_b64, current, region)
        except ValueError as e:
            return f"Ошибка: {e}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"inspect_image: не удалось обработать изображение: {e}")
            return f"Ошибка обработки изображения: {e}"
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Ответь на вопрос только по тому, что реально видно на изображении. "
                    "Если не видно — так и скажи, не додумывай.\nВопрос: " + question)},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
            ],
        }]
        answer = await call_llm_fn(messages, temp=0.2, max_tokens=500)
        return answer or "Модель не вернула ответ по изображению."

    registry.register(
        name="inspect_image",
        description=("Повторно рассматривает изображение из сообщения пользователя. Аргументы: "
                     "question (str — точный вопрос), region (list из 4 чисел 0..1: x0,y0,x1,y1 — "
                     "область для увеличения, опционально). Вызывай несколько раз: обзор, затем детали."),
        parameters={"type": "object", "properties": {
            "question": {"type": "string"},
            "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}},
            "required": ["question"]},
        handler=_inspect, server="internal", timeout_seconds=120)
    logger.info("vision_tools: зарегистрирован inspect_image")
