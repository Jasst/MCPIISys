"""pack_project.py — упаковка проекта для ревью без секретов и runtime-данных.

Запуск из корня проекта:
    python pack_project.py                   # project_for_review.zip рядом с проектом
    python pack_project.py --dry-run         # только показать, что войдёт в архив
    python pack_project.py -o out/review.zip

Правила:
  * исключения точные (по имени, маске или префиксу), а не «все *.txt»;
  * секреты и ключи (.env, *.key, mcp_code.key) исключаются всегда;
  * runtime-данные (ai_memory_v3, user_files, базы, логи) исключаются всегда;
  * кэш и IDE (__pycache__, .idea, .venv, node_modules, .git) исключаются всегда;
  * requirements*.txt, README, документация и прочие нужные .txt остаются.
"""
import argparse
import fnmatch
import sys
import zipfile
from pathlib import Path

# Точные имена файлов (в любом месте дерева).
EXCLUDE_FILE_NAMES = {
    ".env", "auth.db", "assistant.log", "llama_server.log", "mcp_code.key",
    "ее.txt", "tar.py", "pack_project.py",
}
# Маски имён файлов (в любом месте дерева).
EXCLUDE_FILE_GLOBS = ["*.env", ".env.*", "*.key", "*.pem", "*.pfx", "*.pid", "*.log",
                      "*.db", "*.sqlite", "*.sqlite3", "*.zip", "*.pyc", "*.pyo", "*.tsbuildinfo"]
# Имена каталогов (в любом месте дерева): кэш, IDE, данные, зависимости.
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".idea", ".vscode", ".venv", "venv", "node_modules", ".git",
    ".pytest_cache", "ai_memory_v3", "user_files", "ui_media", "ui_state",
    "generated_images", "memory_viewer", "u22", "build", "dist",
}
# Исключения, которые должны уйти только из корня проекта (их имя встречается в коде легитимно).
EXCLUDE_ROOT_ONLY = {"assistant.log"}


# Шаблон переменных окружения нужен ревьюеру: он не содержит секретов.
ALLOW_FILE_NAMES = {".env.example"}


def _is_excluded(rel: Path) -> bool:
    parts = rel.parts
    if rel.name in ALLOW_FILE_NAMES:
        return False
    # каталоги
    for part in parts[:-1]:
        if part in EXCLUDE_DIR_NAMES:
            return True
    name = rel.name
    if name in EXCLUDE_FILE_NAMES:
        return True
    if len(parts) == 1 and name in EXCLUDE_ROOT_ONLY:
        return True
    if any(fnmatch.fnmatch(name, g) for g in EXCLUDE_FILE_GLOBS):
        return True
    # каталог .git и подобные уже отсечены по имени каталога
    return False


def collect(root: Path, output: Path) -> list:
    out_resolved = output.resolve()
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.resolve() == out_resolved:
            continue
        rel = p.relative_to(root)
        if _is_excluded(rel):
            continue
        files.append(rel)
    return files


def pack(root: Path, output: Path, dry_run: bool = False) -> int:
    files = collect(root, output)
    if dry_run:
        for rel in files:
            print(rel.as_posix())
        print(f"\nвойдёт файлов: {len(files)}", file=sys.stderr)
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(root / rel, rel.as_posix())
    print(f"готово: {output} ({len(files)} файлов)", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="корень проекта (по умолчанию текущая папка)")
    ap.add_argument("-o", "--output", default="project_for_review.zip", help="путь к архиву")
    ap.add_argument("--dry-run", action="store_true", help="только список файлов, без архива")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    return pack(root, output, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())