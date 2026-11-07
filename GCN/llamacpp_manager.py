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

from GCN.config_ai import (
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


def _find_model(model_id: str) -> Optional[Dict]:
    for m in scan_models():
        if m["id"] == model_id:
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
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except OSError:
                pass
            self._log_fp = None

    # --- аргументы командной строки ---
    def build_args(self, model_path: str, alias: str, p: LoadParams) -> List[str]:
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
        if LLAMACPP_EXTRA_ARGS:
            args += shlex.split(LLAMACPP_EXTRA_ARGS)
        return args

    # --- загрузка / выгрузка ---
    async def load(self, model_id: str, params: Optional[LoadParams] = None) -> None:
        model = _find_model(model_id)
        if model is None:
            raise RuntimeError(f"Модель {model_id!r} не найдена в {MODELS_DIR}")
        if shutil.which(LLAMACPP_SERVER_BIN) is None and not Path(LLAMACPP_SERVER_BIN).is_file():
            raise RuntimeError(f"llama-server не найден: проверьте LLAMACPP_SERVER_BIN "
                               f"({LLAMACPP_SERVER_BIN})")
        p = (params or get_params(model_id)).normalized()

        await self.unload()                      # один процесс — старый сначала гасим
        args = self.build_args(model["path"], model_id, p)
        env = os.environ.copy()
        env["CUDA_DEVICE_ORDER"] = LLAMACPP_CUDA_DEVICE_ORDER   # порядок карт как в nvidia-smi
        logger.info(f"llama-server: запуск {model_id!r}")
        self._log_fp = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
        self._log_fp.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)}\n")
        self._log_fp.flush()
        creation = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self._proc = subprocess.Popen(args, stdout=self._log_fp, stderr=subprocess.STDOUT,
                                      env=env, creationflags=creation)
        self._model_id, self._params = model_id, p
        try:
            await self._wait_ready()
        except Exception:
            await self.unload()
            raise
        save_params(model_id, p)                 # запомнить удачные параметры

    async def _wait_ready(self) -> None:
        url = f"http://{LLAMACPP_HOST}:{LLAMACPP_PORT}/health"
        deadline = time.monotonic() + LLAMACPP_STARTUP_TIMEOUT
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    raise RuntimeError(f"llama-server завершился (код {self._proc.returncode}). "
                                       f"Подробности в {LLAMACPP_LOG}")
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                        if r.status == 200:
                            return
                except Exception:
                    pass                         # ещё грузится — 503 или нет соединения
                await asyncio.sleep(1.0)
        raise RuntimeError(f"llama-server не ответил за {LLAMACPP_STARTUP_TIMEOUT} с. "
                           f"Смотрите {LLAMACPP_LOG}")

    async def unload(self) -> None:
        proc = self._proc
        self._forget()
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, 15)
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait, 10)
        logger.info("llama-server остановлен, VRAM освобождён")


manager = LlamaServerManager()


async def shutdown_llamacpp() -> None:
    await manager.unload()
