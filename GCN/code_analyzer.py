"""
code_analyzer.py — инструменты для самоанализа кода ИИ-системой.

Позволяет ИИ:
1. Читать файлы своего кода (с проверкой безопасности)
2. Анализировать структуру проекта
3. Искать по коду
4. Предлагать исправления на основе ошибок
"""

import asyncio
import hashlib
import os
import re
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass

from GCN.config_ai import (
    ENABLE_CODE_SELF_REFLECTION,
    CODE_ANALYSIS_MAX_FILES,
    CODE_CONTEXT_MAX_LINES,
    AUTO_FIX_SUGGESTIONS,
    CODE_ACCESS_ROOT,
    ALLOWED_CODE_EXTENSIONS,
    CODE_ALLOWED_DIRS,
)

logger = logging.getLogger(__name__)

MAX_READ_LINES = 1000        # максимум строк за один вызов чтения
DEFAULT_READ_LINES = 400     # по умолчанию
MAX_LIST_FILES = 300         # максимум файлов в списке
MAX_SEARCH_FILES = 300       # сколько файлов проверяет один поиск
MAX_PATTERN_LEN = 200        # защита от слишком сложных regex
SEARCH_CONTEXT = 2           # строк контекста до и после совпадения
CACHE_MAX_FILES = 300        # размер кэша строк файлов


class CodeAccessError(Exception):
    """Ошибка доступа или чтения. code — короткий машинный код, message — для человека."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class CodeSearchResult:
    """Результат поиска в коде."""
    file_path: str
    line_number: int
    line_content: str
    context: str  # несколько строк вокруг


class CodeAnalyzer:
    """Анализатор кода для самообучения ИИ."""

    def __init__(self, root_path: Optional[Path] = None):
        self.root = root_path or CODE_ACCESS_ROOT
        self._file_cache: Dict[str, str] = {}  # кэш содержимого файлов

    # ---------- политика доступа: одно место для всех инструментов ----------
    def _check_path(self, full: Path) -> Path:
        """Проверяет, что файл можно отдавать модели. Возвращает разрешённый путь или бросает CodeAccessError.

        Симлинки разрешаются до проверки, поэтому ссылка из папки кода на данные
        пользователей не обходит запрет. Скрытые файлы, __pycache__ и папки данных закрыты.
        """
        try:
            resolved = full.resolve()
            rel = resolved.relative_to(self.root.resolve())   # выход за корень -> ValueError
        except (ValueError, OSError):
            logger.warning("Доступ к файлу вне проекта запрещён: %s", full)
            raise CodeAccessError("forbidden", "доступ вне корня проекта запрещён")
        if resolved.suffix.lower() not in ALLOWED_CODE_EXTENSIONS:
            raise CodeAccessError("forbidden", "этот тип файла не предоставляется")
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            raise CodeAccessError("forbidden", "скрытые файлы и кэш не предоставляются")
        if len(rel.parts) > 1 and rel.parts[0] not in CODE_ALLOWED_DIRS:
            logger.warning("Доступ к папке данных запрещён: %s", rel.parts[0])
            raise CodeAccessError("forbidden", "папка не входит в код приложения")
        if not resolved.is_file():
            raise CodeAccessError("not_found", "файл не найден")
        return resolved

    def _is_safe_path(self, file_path: str) -> bool:
        """Совместимость со старым кодом: True, если файл разрешён."""
        try:
            self._check_path(Path(file_path))
            return True
        except CodeAccessError:
            return False

    def resolve(self, file_path: str) -> Path:
        """Превращает путь от корня проекта в проверенный абсолютный путь."""
        if not ENABLE_CODE_SELF_REFLECTION:
            raise CodeAccessError("disabled", "самоанализ кода отключён в конфигурации")
        if not isinstance(file_path, str) or not file_path.strip():
            raise CodeAccessError("bad_request", "не указан путь к файлу")
        return self._check_path(self.root / file_path.strip())

    def _rel(self, full: Path) -> str:
        return full.resolve().relative_to(self.root.resolve()).as_posix()

    def _load(self, full: Path):
        """Строки файла и короткий SHA-256 его содержимого.

        Кэш привязан к mtime и размеру: после правки файла следующий запрос
        возвращает новую версию. Раньше кэш жил вечно и отдавал устаревший код.
        """
        try:
            st = full.stat()
        except OSError as e:
            raise CodeAccessError("io_error", f"не удалось открыть файл: {e}")
        key = str(full)
        sig = (st.st_mtime_ns, st.st_size)
        hit = self._file_cache.get(key)
        if hit and hit[0] == sig:
            return hit[1], hit[2]
        try:
            with open(full, "r", encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            raise CodeAccessError("binary", "файл не в кодировке UTF-8, это не текстовый код")
        except OSError as e:
            raise CodeAccessError("io_error", f"не удалось прочитать файл: {e}")
        lines = text.splitlines()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if len(self._file_cache) >= CACHE_MAX_FILES:
            self._file_cache.clear()
        self._file_cache[key] = (sig, lines, digest)
        return lines, digest

    def read_lines(self, file_path: str, start_line: int = 1,
                   max_lines: int = DEFAULT_READ_LINES) -> Dict[str, Any]:
        """Читает кусок файла. Чтобы получить файл целиком, повторяйте вызов с next_start_line.

        sha256 — хэш всего файла. Если он меняется между частями, файл был изменён
        и чтение нужно начать заново.
        """
        full = self.resolve(file_path)
        lines, digest = self._load(full)
        total = len(lines)
        start = max(1, int(start_line))
        count = max(1, min(int(max_lines), MAX_READ_LINES))
        if total and start > total:
            raise CodeAccessError("out_of_range", f"start_line {start} больше числа строк ({total})")
        end = min(total, start + count - 1)
        return {
            "file_path": self._rel(full),
            "total_lines": total,
            "start_line": start,
            "end_line": end,
            "next_start_line": end + 1 if end < total else None,
            "sha256": digest,
            "content": "\n".join(lines[start - 1:end]),
        }

    async def read_file(self, file_path: str, max_lines: int = 200) -> str:
        """Строковый API для внутренних инструментов. Новый код использует read_lines()."""
        try:
            data = await asyncio.to_thread(self.read_lines, file_path, 1, max_lines)
        except CodeAccessError as e:
            return f"Ошибка: {e.message}"
        text = data["content"]
        if data["next_start_line"]:
            text += f"\n\n... (файл обрезан, показано {data['end_line']} из {data['total_lines']} строк)"
        return text

    def list_files(self, max_depth: int = 5, limit: int = MAX_LIST_FILES) -> Dict[str, Any]:
        """Все разрешённые файлы кода: путь, число строк, размер и хэш."""
        if not ENABLE_CODE_SELF_REFLECTION:
            raise CodeAccessError("disabled", "самоанализ кода отключён в конфигурации")
        base = self.root.resolve()
        files: List[Dict[str, Any]] = []
        truncated = False
        for root, dirs, names in os.walk(base):
            level = len(Path(root).relative_to(base).parts)
            keep = []
            for d in sorted(dirs):
                if d.startswith(".") or d == "__pycache__":
                    continue
                if level == 0 and d not in CODE_ALLOWED_DIRS:
                    continue
                if level + 1 > max_depth:
                    continue
                keep.append(d)
            dirs[:] = keep
            for name in sorted(names):
                path = Path(root) / name
                if path.suffix.lower() not in ALLOWED_CODE_EXTENSIONS:
                    continue
                try:
                    full = self._check_path(path)
                    lines, digest = self._load(full)
                except CodeAccessError:
                    continue
                if len(files) >= limit:
                    truncated = True
                    break
                files.append({
                    "file_path": self._rel(full),
                    "lines": len(lines),
                    "size": full.stat().st_size,
                    "sha256": digest,
                })
            if truncated:
                break
        files.sort(key=lambda f: f["file_path"])
        return {"files": files, "count": len(files), "truncated": truncated}

    def search_structured(self, pattern: str, max_results: int = 20) -> Dict[str, Any]:
        """Поиск по коду с контекстом. files_truncated=True — проверены не все файлы."""
        if not ENABLE_CODE_SELF_REFLECTION:
            raise CodeAccessError("disabled", "самоанализ кода отключён в конфигурации")
        if not isinstance(pattern, str) or not pattern.strip():
            raise CodeAccessError("bad_request", "пустой запрос поиска")
        if len(pattern) > MAX_PATTERN_LEN:
            raise CodeAccessError("bad_request", f"запрос длиннее {MAX_PATTERN_LEN} символов")
        try:
            regex = re.compile(pattern, re.IGNORECASE)
            needle = None
        except re.error:
            regex = None
            needle = pattern.lower()

        listing = self.list_files(max_depth=10, limit=MAX_SEARCH_FILES)
        base = self.root.resolve()
        results: List[Dict[str, Any]] = []
        for item in listing["files"]:
            try:
                lines, _ = self._load(base / item["file_path"])
            except CodeAccessError:
                continue
            for idx, line in enumerate(lines):
                hit = regex.search(line) if regex else (needle in line.lower())
                if not hit:
                    continue
                lo = max(0, idx - SEARCH_CONTEXT)
                hi = min(len(lines), idx + SEARCH_CONTEXT + 1)
                results.append({
                    "file_path": item["file_path"],
                    "line": idx + 1,
                    "text": line.strip()[:200],
                    "context": "\n".join(lines[lo:hi]),
                })
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break
        return {
            "results": results,
            "files_scanned": len(listing["files"]),
            "files_truncated": listing["truncated"],
            "results_limited": len(results) >= max_results,
        }

    async def search_in_code(self, pattern: str, max_results: int = 20) -> str:
        """Строковый API для внутренних инструментов. Новый код использует search_structured()."""
        try:
            data = await asyncio.to_thread(self.search_structured, pattern, max_results)
        except CodeAccessError as e:
            return f"Ошибка: {e.message}"
        if not data["results"]:
            return f"Ничего не найдено по запросу '{pattern}'."
        out = [f"Найдено {len(data['results'])} совпадений по запросу '{pattern}':"]
        for i, r in enumerate(data["results"], 1):
            out.append(f"\n{i}. Файл: {r['file_path']}, строка {r['line']}")
            out.append(f"   {r['text']}")
        if data["files_truncated"]:
            out.append("\n(поиск неполный: проверены не все файлы проекта)")
        return "\n".join(out)

    async def get_project_structure(self, max_depth: int = 3) -> str:
        """
        Возвращает структуру проекта в виде дерева.

        Args:
            max_depth: Максимальная глубина обхода

        Returns:
            Текстовое представление структуры
        """
        if not ENABLE_CODE_SELF_REFLECTION:
            return "Самоанализ кода отключён в конфигурации."

        def build_tree(path: Path, prefix: str = "", depth: int = 0) -> List[str]:
            if depth > max_depth:
                return []

            result = []
            try:
                items = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
                items = [item for item in items if not item.name.startswith('.') and item.name != '__pycache__']
                if path.resolve() == self.root.resolve():
                    items = [i for i in items if (i.is_dir() and i.name in CODE_ALLOWED_DIRS)
                             or (i.is_file() and i.suffix.lower() in ALLOWED_CODE_EXTENSIONS)]

                for i, item in enumerate(items):
                    is_last = (i == len(items) - 1)
                    connector = "└── " if is_last else "├── "

                    if item.is_file():
                        if item.suffix.lower() in ALLOWED_CODE_EXTENSIONS:
                            result.append(f"{prefix}{connector}{item.name}")
                    else:
                        result.append(f"{prefix}{connector}{item.name}/")
                        extension = "    " if is_last else "│   "
                        result.extend(build_tree(item, prefix + extension, depth + 1))
            except PermissionError:
                pass

            return result

        tree_lines = build_tree(self.root)
        return f"Структура проекта (до глубины {max_depth}):\n\n" + "\n".join(tree_lines)

    async def analyze_error_location(self, error_message: str, traceback_str: str) -> str:
        """
        Анализирует ошибку и пытается найти проблемное место в коде.

        Args:
            error_message: Текст ошибки
            traceback_str: Трассировка стека

        Returns:
            Анализ ошибки с предложениями по исправлению
        """
        if not ENABLE_CODE_SELF_REFLECTION:
            return "Самоанализ кода отключён в конфигурации."

        # Извлекаем имена файлов и строки из traceback
        file_pattern = r'File "([^"]+)", line (\d+)'
        matches = re.findall(file_pattern, traceback_str)

        if not matches:
            return f"Не удалось извлечь информацию о местоположении ошибки из traceback.\n\nОшибка: {error_message}"

        analysis_parts = [f"Анализ ошибки: {error_message}\n"]
        analysis_parts.append("=" * 60)

        for file_path, line_num in matches[:3]:  # Берём первые 3 локации
            # Пытаемся найти относительный путь
            try:
                abs_path = Path(file_path).resolve()
                # ИСПРАВЛЕНИЕ (пункт 4): используем relative_to вместо startswith.
                # Внутренний try закрывает только случай "путь вне корня проекта"
                # (relative_to → ValueError), чтобы не путать его с реальными
                # ошибками чтения файла. Без парного except внутренний try
                # оставлял внешний try без except → SyntaxError.
                try:
                    rel_path_obj = abs_path.relative_to(self.root.resolve())
                except ValueError:
                    logger.debug(f"Путь {file_path} вне корня проекта — пропуск локации")
                    continue
                rel_path = str(rel_path_obj)
                if not self._is_safe_path(str(abs_path)):
                    continue

                # Читаем файл с контекстом
                content = await self.read_file(rel_path, max_lines=int(line_num) + 10)

                analysis_parts.append(f"\n📁 Файл: {rel_path}, строка {line_num}")
                # Выделяем проблемную строку
                lines = content.split('\n')
                if int(line_num) <= len(lines):
                    target_line = lines[int(line_num) - 1]
                    analysis_parts.append(f"Код:\n{target_line}")

                if AUTO_FIX_SUGGESTIONS:
                    analysis_parts.append("\n💡 Возможные причины:")

                    # Простые эвристики для типичных ошибок
                    if "IndexError" in error_message:
                        analysis_parts.append("  - Выход за пределы списка/массива")
                        analysis_parts.append("  - Проверь границы индекса перед доступом")
                    elif "KeyError" in error_message:
                        analysis_parts.append("  - Доступ к несуществующему ключу в словаре")
                        analysis_parts.append("  - Используй .get() или проверку in перед доступом")
                    elif "AttributeError" in error_message:
                        analysis_parts.append("  - Доступ к несуществующему атрибуту объекта")
                        analysis_parts.append("  - Проверь тип объекта и доступные методы")
                    elif "TypeError" in error_message:
                        analysis_parts.append("  - Несоответствие типов аргументов")
                        analysis_parts.append("  - Проверь сигнатуры функций и типы данных")
                    elif "NameError" in error_message:
                        analysis_parts.append("  - Использование необъявленной переменной")
                        analysis_parts.append("  - Проверь импорты и область видимости")
                    elif "ImportError" in error_message or "ModuleNotFoundError" in error_message:
                        analysis_parts.append("  - Модуль не найден")
                        analysis_parts.append("  - Проверь установку зависимостей (pip install)")
                    else:
                        analysis_parts.append("  - Требуется ручной анализ кода")

            except Exception as e:
                logger.debug(f"Не удалось проанализировать файл {file_path}: {e}")

        if AUTO_FIX_SUGGESTIONS:
            analysis_parts.append("\n" + "=" * 60)
            analysis_parts.append("\n🔧 Общие рекомендации:")
            analysis_parts.append("  1. Проверь типы данных всех переменных")
            analysis_parts.append("  2. Добавь обработку исключений в критичных местах")
            analysis_parts.append("  3. Убедись, что все зависимости установлены")
            analysis_parts.append("  4. Проверь соответствие версий библиотек")

        return "\n".join(analysis_parts)

    def clear_cache(self):
        """Очищает кэш файлов."""
        self._file_cache.clear()


# Глобальный экземпляр (ленивая инициализация)
_analyzer: Optional[CodeAnalyzer] = None


def get_analyzer() -> CodeAnalyzer:
    """Возвращает глобальный экземпляр анализатора."""
    global _analyzer
    if _analyzer is None:
        _analyzer = CodeAnalyzer()
    return _analyzer