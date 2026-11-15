"""Сборка реестра инструментов чата целиком (как в CognitiveController.__init__).
Ловит NameError при регистрации и инструменты, у которых нет обработчика.
Запуск: python tests/test_registry_build.py"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from GCN.tool_router import ToolRegistry  # noqa: E402
from GCN.internal_tools import register_all  # noqa: E402


class _Store:
    pass


class _Mem:
    def __init__(self):
        self.private_memory = SimpleNamespace(gcn_store=_Store())
        self.shared_memory = SimpleNamespace(gcn_store=_Store())

    def __getattr__(self, name):
        return lambda *a, **k: None


def main():
    reg = ToolRegistry()
    ctrl = SimpleNamespace(memory_service=_Mem(), history=[], user_id="test")
    register_all(reg, ctrl, query_expander=None, query_refiner=None)
    names = [t.qualified_name for t in reg._visible()]
    assert names, "реестр пуст"
    for n in names:
        spec = reg.get(n)
        assert callable(spec.handler), f"нет обработчика: {n}"
    for dead in ("internal__contribute_to_identity", "internal__merge_identity_branches"):
        assert dead not in names, f"мёртвый инструмент всё ещё в чате: {dead}"
    for needed in ("internal__identity_propose", "internal__identity_commit",
                   "internal__get_identity_chain", "internal__search_identity_chain",
                   "internal__invalidate_identity", "internal__remember", "internal__recall"):
        assert needed in names, f"нет инструмента: {needed}"
    print(f"инструментов в чате: {len(names)}")
    print("REGISTRY BUILD PASSED")


if __name__ == "__main__":
    main()
