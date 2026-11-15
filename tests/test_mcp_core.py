"""MCP: общее ядро меняется только через core_command (та же логика, что в чате).
Мёртвые инструменты записи личности удалены из MCP: contribute_to_identity, merge_identity_branches,
invalidate_identity. Личная запись — только в чате (identity_propose → identity_commit).
Нужны зависимости MCP-сервера; без них тест пропускается.
Запуск: python tests/test_mcp_core.py"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MCP_ADMIN_USERS"] = "u2"
try:
    import GCN.core_admin as ca
    import GCN.GCN as G
    import mcp_server_blockcoin as M
except ImportError as e:
    print(f"SKIP: нужны зависимости MCP-сервера ({e})")
    sys.exit(0)

ca.STORE_PATH = Path(tempfile.mkdtemp()) / "core_admin.json"
GOOD = "Я стараюсь говорить ясно и честно, даже когда тема мне не близка."
CALLS = []


async def fake_append(service, content, contributor_model, **k):
    CALLS.append(k.get("layer"))
    return {"status": "ok", "id": f"s{len(CALLS)}"}


G.append_snapshot = fake_append
G.get_latest_head = lambda store: None


async def fake_mem(uid):
    class S:
        shared_memory = type("X", (), {"gcn_store": object()})()
    return S()


M.get_memory_service = fake_mem


def tool(t):
    return getattr(t, "fn", t)


async def _run():
    # мёртвые инструменты записи личности удалены из MCP полностью
    for dead in ("contribute_to_identity", "merge_identity_branches", "invalidate_identity"):
        assert not hasattr(M, dead), f"{dead} должен быть удалён из MCP"

    p = await tool(M.core_command)(message="ядро: " + GOOD, user_id="u2")
    assert p["status"] == "ok" and GOOD in p["reply"] and CALLS == [], "предложение ничего не меняет"
    a = await tool(M.core_command)(message="да", user_id="u2")
    assert a["reply"].startswith("Применено") and CALLS == ["shared"], CALLS
    n = await tool(M.core_command)(message="да", user_id="alice")
    assert n["status"] == "error" and len(CALLS) == 1, "не-администратор не меняет ядро"


if __name__ == "__main__":
    asyncio.run(_run())
    print("ALL MCP CORE CHECKS PASSED")
