"""Старт в режиме llama.cpp: сколько раз запускается llama-server.
Проверяет: автозапуск + выбор той же модели из UI (с параметрами и без) = ОДИН запуск;
LLAMACPP_DEFAULT_MODEL с обратными слэшами находит модель в подпапке.
Запуск: python tests/test_llamacpp_startup.py"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODELS = Path(tempfile.mkdtemp())
(MODELS / "sub").mkdir()
(MODELS / "demo-model.gguf").write_bytes(b"x")
(MODELS / "sub" / "nested.gguf").write_bytes(b"x")
os.environ.update({"LLM_BACKEND": "llamacpp", "LLAMACPP_AUTOSTART": "1", "MODELS_DIR": str(MODELS),
                   "LLAMACPP_SERVER_BIN": "/bin/true", "LLAMACPP_DEFAULT_MODEL": "demo-model.gguf",
                   "LM_STUDIO_MODEL": "demo-model.gguf"})

import GCN.llamacpp_manager as lm  # noqa: E402
import GCN.llm_client as llm  # noqa: E402

lm.LOG_PATH = MODELS / "llama.log"
lm.PARAMS_FILE = MODELS / "model_params.json"
STARTS = []


class FakeProc:
    def __init__(self, args, **kw):
        STARTS.append(args[args.index("-m") + 1])
        self.returncode = None

    def poll(self):
        return None


async def _ready(self):
    await asyncio.sleep(0.2)


async def _unload(self):
    self._proc = None
    self._model_id = None
    self._params = None


lm.subprocess.Popen = FakeProc
lm.LlamaServerManager._wait_ready = _ready
lm.LlamaServerManager.unload = _unload
lm.save_params = lambda *a, **k: None


async def scenario_single_start():
    STARTS.clear()
    target = "demo-model.gguf"
    await llm.ensure_model_loaded(target)                 # автозапуск
    await llm.ensure_model_loaded(target, None)           # страница: выбор без параметров
    saved = lm.get_params(target)
    params = {"ctx": saved.ctx, "gpu_layers": saved.gpu_layers, "flash_attn": saved.flash_attn,
              "kv_cache_type": saved.kv_cache_type, "threads": saved.threads,
              "main_gpu": saved.main_gpu, "tensor_split": saved.tensor_split, "parallel": saved.parallel}
    await llm.ensure_model_loaded(target, params)         # окно настроек с теми же значениями
    assert len(STARTS) == 1, f"ожидался 1 запуск llama-server, было {len(STARTS)}: {STARTS}"
    print("1 ok: автозапуск + повторный выбор той же модели = один запуск llama-server")


def scenario_backslash_default():
    assert lm.normalize_model_id("sub\\nested.gguf") == "sub/nested.gguf"
    assert lm.normalize_model_id("./demo-model.gguf") == "demo-model.gguf"
    found = lm._find_model(lm.normalize_model_id("sub\\nested.gguf"))
    assert found is not None and found["id"] == "sub/nested.gguf"
    print("2 ok: LLAMACPP_DEFAULT_MODEL с обратными слэшами находит модель в подпапке")


async def main():
    await scenario_single_start()
    scenario_backslash_default()
    print("ALL LLAMACPP STARTUP CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
