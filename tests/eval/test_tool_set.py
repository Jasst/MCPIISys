"""
Проверка набора инструментов для чата (без запуска модели и сервера).
Запуск из папки проекта: python tests/eval/test_tool_set.py

Проверяет: исключения ссылаются на существующие инструменты (нет опечаток),
в чате нет двух инструментов для одного действия, общее число не разрастается.
"""
import ast
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
os.chdir(ROOT)

cfg = open('GCN/config_ai.py', encoding='utf-8').read()
start = cfg.index('CHAT_TOOL_EXCLUDE = {')
exclude = set(re.findall(r'"([a-z_]+)"', cfg[start:cfg.index('}', start)]))

mcp_src = open('mcp_server_blockcoin.py', encoding='utf-8').read()
mcp_tools = [n.name for n in ast.parse(mcp_src).body
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
             and any('mcp.tool' in ast.unparse(d) for d in n.decorator_list)]
LOGIN_ONLY = {"identify", "request_login", "verify_login", "login", "logout"}

internal = set(re.findall(r'name="([a-z_]+)"', open('GCN/internal_tools.py', encoding='utf-8').read()))

# Внутренние инструменты с тем же действием, что и MCP-инструмент
TWINS = {
    'recall': 'recall', 'remember': 'remember', 'add_goal': 'add_goal', 'web_search': 'web_search',
    'generate_image': 'generate_image', 'read_code_file': 'read_code', 'search_in_code': 'search_code',
    'get_project_structure': 'project_structure', 'analyze_error': 'analyze_error',
    'fetch_github_file': 'fetch_github_file',
    'remember_batch': 'remember', 'semantic_search': 'recall',
}

res = []
def check(name, ok, extra=''):
    res.append(bool(ok)); print('PASS' if ok else 'FAIL', name, extra)

check('1 every excluded name exists in MCP (no typos)',
      exclude <= set(mcp_tools) | LOGIN_ONLY or not (exclude - set(mcp_tools)),
      str(sorted(exclude - set(mcp_tools))))

missing_twin = [m for m in TWINS if m in set(mcp_tools) and m not in exclude]
check('2 MCP duplicates of internal tools are excluded from chat', not missing_twin, str(missing_twin))

kept = [n for n in mcp_tools if n not in exclude and n not in LOGIN_ONLY]
dup = [n for n in kept if n in internal or n in TWINS.values()]
check('3 no kept MCP tool duplicates an internal tool', not dup, str(dup))

total = len(kept) + len(internal)
check('4 chat tool count stays reasonable (<= 36)', total <= 36, f'total={total}')

passed = sum(res)
print(f'\n{passed}/{len(res)} passed')
sys.exit(0 if passed == len(res) else 1)
