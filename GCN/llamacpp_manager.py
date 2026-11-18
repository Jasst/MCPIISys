"""
Прямая загрузка GGUF-моделей через llama-server (llama.cpp) без LM Studio.

Модель грузится так: приложение запускает llama-server как дочерний процесс
с нужными аргументами (путь к GGUF, контекст, слои на GPU, KV-кэш и т.д.),
ждёт ответа /health и дальше ходит в него по OpenAI-совместимому API.
Выгрузка = остановка процесса: VRAM освобождается полностью.

Одна модель на процесс. Параметры загрузки хранятся для каждой модели
отдельно в model_params.json (рядом с проектом, уже в .gitignore).
"""
import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

from .config_ai import (
    LLAMACPP_CUDA_DEVICE_ORDER,
    LLAMACPP_DEFAULTS,
    LLAMACPP_EXTRA_ARGS,
    LLAMACPP_HOST,
    LLAMACPP_LOG,
    LLAMACPP_PORT,
    LLAMACPP_SERVER_BIN,
    LLAMACPP_STARTUP_TIMEOUT,
    MODELS_DIR,
)

logger = logging.getLogger(__name__)
startup_logger = logging.getLogger("startup")  # Для отслеживания старта llama-server

BASE_DIR = Path(__file__).resolve().parent.parent
PARAMS_FILE = BASE_DIR / "model_params.json"
LOG_PATH = BASE_DIR / LLAMACPP_LOG

KV_TYPES = ("f16", "q8_0", "q4_0")


@dataclass
class LoadParams:
    ctx: int = 8192              # размер контекста (-c)
    gpu_layers: int = -1         # слоёв на GPU (-ngl); -1 = все
    flash_attn: bool = True      # flash attention (-fa); нужен для квантованного KV
    kv_cache_type: str = "q8_0"  # тип KV-кэша: f16 | q8_0 | q4_0
    threads: int = 14            # потоки CPU (-t)
    main_gpu: int = 0            # основная карта (--main-gpu)
    tensor_split: str = ""       # доля по картам, например "12,6"; пусто = авто
    parallel: int = 1            # слотов параллельных запросов (-np)

    def normalized(self) -> "LoadParams":
        """Приводит типы и проверяет диапазоны; бросает ValueError при мусоре."""
        p = LoadParams(
            ctx=int(self.ctx), gpu_layers=int(self.gpu_layers),
            flash_attn=bool(self.flash_attn), kv_cache_type=str(self.kv_cache_type),
            threads=int(self.threads), main_gpu=int(self.main_gpu),
            tensor_split=str(self.tensor_split or "").strip(), parallel=int(self.parallel),
        )
        if not 512 <= p.ctx <= 262144:
            raise ValueError("ctx должен быть от 512 до 262144")
        if p.gpu_layers < -1:
            raise ValueError("gpu_layers: -1 (все) или число >= 0")
        if p.kv_cache_type not in KV_TYPES:
            raise ValueError(f"kv_cache_type: один из {', '.join(KV_TYPES)}")
        if not 1 <= p.threads <= 128:
            raise ValueError("threads должен быть от 1 до 128")
        if not 0 <= p.main_gpu <= 15:
            raise ValueError("main_gpu должен быть от 0 до 15")
        if not 1 <= p.parallel <= 8:
            raise ValueError("parallel должен быть от 1 до 8")
        if p.tensor_split and not re.fullmatch(r"\d+(\.\d+)?(,\d+(\.\d+)?)*", p.tensor_split):
            raise ValueError("tensor_split: числа через запятую, например 12,6")
        return p


def defaults() -> LoadParams:
    return LoadParams(**LLAMACPP_DEFAULTS).normalized()


# ---------- сканирование папки с моделями ----------

def model_id_for(path: Path) -> str:
    """id модели = путь относительно MODELS_DIR, с прямыми слэшами."""
    return path.relative_to(Path(MODELS_DIR)).as_posix()


def scan_models() -> List[Dict]:
    """Все .gguf в MODELS_DIR (рекурсивно). Пропускает mmproj (проекторы
    vision) и вторые+ части разбитых файлов (…-00002-of-00003.gguf)."""
    if not MODELS_DIR:
        return []
    root = Path(MODELS_DIR)
    if not root.is_dir():
        logger.warning(f"MODELS_DIR не найден: {root}")
        return []
    out = []
    for p in sorted(root.rglob("*.gguf")):
        name = p.name.lower()
        if "mmproj" in name:
            continue
        m = re.search(r"-(\d{5})-of-\d{5}\.gguf$", name)
        if m and m.group(1) != "00001":
            continue
        try:
            size_gb = round(p.stat().st_size / 1024 ** 3, 2)
        except OSError:
            continue
        out.append({"id": model_id_for(p), "path": str(p), "size_gb": size_gb})
    return out


def find_mmproj(model_path: str) -> Optional[str]:
    """Проектор зрения (mmproj*.gguf) рядом с моделью или None.
    Без --mmproj llama-server отвечает 'image input is not supported' на любое фото."""
    p = Path(model_path)
    cands = [f for f in p.parent.glob("*.gguf") if "mmproj" in f.name.lower()]
    if not cands:
        return None
    if len(cands) == 1:
        return str(cands[0])

    def tokens(s: str) -> set:
        return set(re.findall(r"[a-z0-9]+", s.lower())) - {"mmproj", "gguf"}

    model_tokens = tokens(p.stem)
    best = max(cands, key=lambda f: len(tokens(f.stem) & model_tokens))
    return str(best)


def normalize_model_id(value: str) -> str:
    """Приводит id модели к виду, в котором его отдаёт scan_models(): прямые слэши,
    без ведущего './' и пробелов. Так LLAMACPP_DEFAULT_MODEL, записанный на Windows
    как 'sub\\model.gguf', совпадает с реальным id 'sub/model.gguf'."""
    v = (value or "").strip().replace("\\", "/")
    while v.startswith("./"):
        v = v[2:]
    return v


def _find_model(model_id: str) -> Optional[Dict]:
    want = normalize_model_id(model_id)
    for m in scan_models():
        if m["id"] == want:
            return m
    return None


# ---------- параметры по моделям (model_params.json) ----------

def _read_store() -> Dict[str, Dict]:
    try:
        import json
        return json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def get_params(model_id: str) -> LoadParams:
    """Сохранённые параметры модели; для незнакомой — значения по умолчанию из .env."""
    base = asdict(defaults())
    saved = _read_store().get(model_id) or {}
    base.update({k: v for k, v in saved.items() if k in base})
    return LoadParams(**base).normalized()


def has_saved_params(model_id: str) -> bool:
    return model_id in _read_store()


def save_params(model_id: str, params: LoadParams) -> None:
    import json
    store = _read_store()
    store[model_id] = asdict(params.normalized())
    tmp = PARAMS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PARAMS_FILE)


# ---------- процесс llama-server ----------

class LlamaServerManager:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._model_id: Optional[str] = None
        self._params: Optional[LoadParams] = None
        self._mmproj: Optional[str] = None
        self._log_fp = None

    # --- состояние ---
    def loaded_id(self) -> Optional[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._model_id
        if self._proc is not None:          # процесс умер сам (OOM, краш)
            logger.error(f"llama-server завершился с кодом {self._proc.returncode}")
            self._forget()
        return None

    def params(self) -> Optional[LoadParams]:
        return self._params if self.loaded_id() else None

    def _forget(self) -> None:
        self._proc = None
        self._model_id = None
        self._params = None
        self._mmproj = None
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except OSError:
                pass
            self._log_fp = None

    # --- аргументы командной строки ---
    def build_args(self, model_path: str, alias: str, p: LoadParams,
                   mmproj: Optional[str] = None) -> List[str]:
        # квантованный KV-кэш в llama.cpp требует flash attention
        fa = p.flash_attn or p.kv_cache_type != "f16"
        args = [
            LLAMACPP_SERVER_BIN,
            "-m", model_path,
            "--alias", alias,                    # имя модели в API = наш id
            "--host", LLAMACPP_HOST,
            "--port", str(LLAMACPP_PORT),
            "-c", str(p.ctx),
            "-ngl", str(999 if p.gpu_layers < 0 else p.gpu_layers),
            "-fa", "on" if fa else "off",
            "-t", str(p.threads),
            "-np", str(p.parallel),
            "--main-gpu", str(p.main_gpu),
            "--jinja",                           # нативный tool calling (chat-шаблон модели)
            "--reasoning-format", "deepseek",    # thinking приходит в reasoning_content
        ]
        if p.kv_cache_type != "f16":
            args += ["--cache-type-k", p.kv_cache_type, "--cache-type-v", p.kv_cache_type]
        if p.tensor_split:
            args += ["--tensor-split", p.tensor_split]
        if mmproj:
            args += ["--mmproj", mmproj]         # включает распознавание изображений
        if LLAMACPP_EXTRA_ARGS:
            args += shlex.split(LLAMACPP_EXTRA_ARGS)
        return args

    # --- загрузка / выгрузка ---
    async def load(self, model_id: str, params: Optional[LoadParams] = None) -> None:
        """Загружает модель llama-server.
        
        Аргументы:
            model_id — идентификатор модели из MODELS_DIR
            params   — параметры загрузки (опционально, по умолчанию из model_params.json)
            
        Логирование:
            - Запись в файл LLAMACPP_LOG с аргументами запуска
            - Отслеживание состояния процесса и ответа /health
        """
        model = _find_model(model_id)
        if model is None:
            raise RuntimeError(f"Модель {model_id!r} не найдена в {MODELS_DIR}")
        
        # Проверка llama-server бинарника
        server_bin_path = Path(LLAMACPP_SERVER_BIN).resolve()
        if shutil.which(LLAMACPP_SERVER_BIN) is None and not server_bin_path.is_file():
            raise RuntimeError(f"llama-server не найден: проверьте LLAMACPP_SERVER_BIN "
                               f"({LLAMACPP_SERVER_BIN}, путь: {server_bin_path})")

        p = (params or get_params(model_id)).normalized()

        # Поиск mmproj проектора
        mmproj = find_mmproj(model["path"])
        startup_logger.info(f"[LLAMA] Модель {model_id!r}: mmproj={mmproj or 'не найден'}")
        
        # Формирование аргументов запуска
        args = self.build_args(model["path"], model_id, p, mmproj=mmproj)
        startup_logger.info(f"[LLAMA] Команда запуска: {' '.join(args)}")

        # Подготовка окружения
        env = os.environ.copy()
        env["CUDA_DEVICE_ORDER"] = LLAMACPP_CUDA_DEVICE_ORDER   # порядок карт как в nvidia-smi
        
        # Информация о модели для лога
        model_info = f"ctx={p.ctx}, gpu_layers={p.gpu_layers if p.gpu_layers >= 0 else 'все'}, " \
                    f"flash_attn={'on' if p.flash_attn else 'off'}, kv_cache={p.kv_cache_type}, " \
                    f"threads={p.threads}, parallel={p.parallel}, main_gpu={p.main_gpu}"
        startup_logger.info(f"[LLAMA] Параметры: {model_info}")

        # Открываем файл логов llama-server
        self._log_fp = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        args_str = ' '.join(args)
        self._log_fp.write(f"\n===== {timestamp} {' '.join(args)}\n")
        self._log_fp.flush()

        # Запуск subprocess с CREATE_NEW_PROCESS_GROUP для Windows (Ctrl+C работает корректно)
        creation = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        startup_logger.info(f"[LLAMA] Запуск llama-server: PID={self._proc.pid if self._proc else 'pending'}")

        self._proc = subprocess.Popen(args, stdout=self._log_fp, stderr=subprocess.STDOUT,
                                      env=env, creationflags=creation)
        self._model_id, self._params = model_id, p
        self._mmproj = mmproj
        
        # Записываем PID в лог для отслеживания
        startup_logger.info(f"[LLAMA] llama-server запущен с PID={self._proc.pid}")

        try:
            await self._wait_ready()
        except Exception as e:
            startup_logger.error(f"[LLAMA] Ошибка при ожидании готовности: {e}", exc_info=True)
            await self.unload()
            raise
        
        # Сохраняем удачные параметры для этой модели
        save_params(model_id, p)
        startup_logger.info(f"[LLAMA] Параметры сохранения в model_params.json завершены")

    async def _wait_ready(self) -> None:
        """Ожидает ответа /health от llama-server.
        
        Аргументы:
            url      — URL для проверки здоровья (/health)
            deadline — время дедлайна (текущее + LLAMACPP_STARTUP_TIMEOUT)
            
        Возвращает, когда сервер ответил 200 OK на /health
        
        Логирование:
            - Каждую секунду проверяем статус процесса и HTTP-ответ
            - При успехе — записываем в лог
            - При ошибке — логируем с полным стеком
        """
        url = f"http://{LLAMACPP_HOST}:{LLAMACPP_PORT}/health"
        deadline = time.monotonic() + LLAMACPP_STARTUP_TIMEOUT
        
        startup_logger.info(f"[LLAMA] Ожидание готовности llama-server: {url} (дедлайн через {LLAMACPP_STARTUP_TIMEOUT:.1f}с)")

        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                # Проверка состояния процесса
                if self._proc.poll() is not None:
                    returncode = self._proc.returncode
                    startup_logger.error(f"[LLAMA] llama-server завершился с кодом {returncode}!", exc_info=True)
                    raise RuntimeError(f"llama-server завершился (код {returncode}). "
                                       f"Подробности в {LLAMACPP_LOG}")

                # Проверка HTTP-ответа /health
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                        if r.status == 200:
                            startup_logger.info(f"[LLAMA] llama-server готов! Ответ /health: {r.status}")
                            return
                except Exception as e:
                    # Это нормально — сервер ещё грузится, может быть 503 или нет соединения
                    pass

                await asyncio.sleep(1.0)

        startup_logger.error(f"[LLAMA] Ошибка: llama-server не ответил за {LLAMACPP_STARTUP_TIMEOUT:.1f}с", exc_info=True)
        raise RuntimeError(f"llama-server не ответил за {LLAMACPP_STARTUP_TIMEOUT:.1f} с. "
                           f"Смотрите {LLAMACPP_LOG}")

    async def unload(self) -> None:
        """Останавливает llama-server и освобождает VRAM.
        
        Логирование:
            - Запись о завершении процесса
            - Успешное освобождение VRAM
        """
        proc = self._proc
        self._forget()
        
        if proc is None or proc.poll() is not None:
            startup_logger.info(f"[LLAMA] llama-server уже остановлен (PID={proc.pid if proc else 'N/A'})")
            return

        # graceful terminate с таймаутом 15с
        startup_logger.info(f"[LLAMA] Завершение llama-server (PID={proc.pid})...")
        proc.terminate()
        
        try:
            await asyncio.to_thread(proc.wait, 15)
        except subprocess.TimeoutExpired:
            # force kill если процесс не остановился
            startup_logger.warning(f"[LLAMA] graceful terminate не сработал, force kill (PID={proc.pid})")
            proc.kill()
            await asyncio.to_thread(proc.wait, 10)

        startup_logger.info("[LLAMA] llama-server остановлен, VRAM освобождён")


manager = LlamaServerManager()


async def shutdown_llamacpp() -> None:
    await manager.unload()