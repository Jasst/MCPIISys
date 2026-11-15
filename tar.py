import zipfile, pathlib
EXCLUDE_NAMES = {'.env', 'auth.db', 'assistant.log', 'mcp_code.key', 'llama_server.log',
                 '__pycache__', '.idea', '.venv', 'node_modules', 'ее.txt', '.git'}
EXCLUDE_DIRS = {'ai_memory_v3', 'u22', 'ui_media', 'ui_state', 'user_files', 'generated_images'}
EXCLUDE_SUFFIX = {'.pyc', '.pid', '.log', '.db', '.zip'}
root = pathlib.Path('.')
with zipfile.ZipFile('project_for_review.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for p in root.rglob('*'):
        if not p.is_file():
            continue
        parts = set(p.relative_to(root).parts)
        if parts & EXCLUDE_NAMES or parts & EXCLUDE_DIRS or p.suffix in EXCLUDE_SUFFIX or p.name.endswith('.txt'):
            continue
        z.write(p, p.relative_to(root).as_posix())