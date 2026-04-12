"""
Purpose: Lightweight AST-based code index for Python files in a repository.
         Extracts classes, functions, and their line ranges without external dependencies.
         Used by the coding worker to prepare compact, targeted context for LLM edit operations.
Input/Output: Given a repo path and a list of file paths, returns a CodeIndex with per-file symbol info.
Important invariants: Read-only; filesystem and git remain the source of truth. Index is never persisted.
How to debug: If symbol lookup fails, check that the file is valid Python and symbol names match exactly.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SymbolInfo:
    """Metadata for a top-level or nested symbol extracted from a Python source file."""

    name: str
    kind: str  # "function" | "async_function" | "class" | "method" | "async_method"
    start_line: int  # 1-indexed, inclusive (includes decorators)
    end_line: int  # 1-indexed, inclusive
    parent: str | None = None  # class name for methods, None for top-level symbols


@dataclass
class FileIndex:
    """Per-file analysis result containing extracted symbols and basic metadata."""

    path: str  # relative to repo root
    content_hash: str  # SHA-256 hex prefix of file bytes
    total_lines: int
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: str | None = None  # set if ast.parse failed


class CodeIndex:
    """In-memory index of symbols across a set of files in a repository."""

    def __init__(self, repo_path: Path, files: dict[str, FileIndex]) -> None:
        self.repo_path = repo_path
        self.files: dict[str, FileIndex] = files  # relative path → FileIndex

    def get_file(self, file_path: str) -> FileIndex | None:
        return self.files.get(file_path)

    def get_symbol(self, file_path: str, symbol_name: str) -> SymbolInfo | None:
        """Return the first symbol with the given name in the given file."""
        fi = self.files.get(file_path)
        if fi is None:
            return None
        for sym in fi.symbols:
            if sym.name == symbol_name:
                return sym
        return None

    def get_symbol_content(self, file_path: str, symbol_name: str) -> str | None:
        """Extract the exact source lines for a named symbol from the real file."""
        sym = self.get_symbol(file_path, symbol_name)
        if sym is None:
            return None
        full_path = self.repo_path / file_path
        if not full_path.is_file():
            return None
        lines = full_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
        return "".join(lines[sym.start_line - 1 : sym.end_line])

    def format_for_prompt(self, max_symbols_per_file: int = 40) -> str:
        """Return a compact symbol index suitable for inclusion in an LLM prompt."""
        if not self.files:
            return ""
        parts: list[str] = ["Symbol index (use for replace_symbol_body or replace_lines):"]
        for path, fi in sorted(self.files.items()):
            if fi.parse_error:
                parts.append(f"  {path} [{fi.total_lines} lines, not parseable as Python]")
                continue
            header = f"  {path} [{fi.total_lines} lines]:"
            parts.append(header)
            for sym in fi.symbols[:max_symbols_per_file]:
                parent_suffix = f" (in {sym.parent})" if sym.parent else ""
                parts.append(f"    - {sym.name} [{sym.kind}, lines {sym.start_line}-{sym.end_line}]{parent_suffix}")
            if len(fi.symbols) > max_symbols_per_file:
                parts.append(f"    ... and {len(fi.symbols) - max_symbols_per_file} more")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------


def _file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


def _node_end_line(node: ast.AST) -> int:
    """Return a stable inclusive end line even when Python's AST omits end_lineno."""

    end_line = getattr(node, "end_lineno", None)
    start_line = getattr(node, "lineno", 1)
    return end_line if isinstance(end_line, int) else int(start_line)


def _index_python_file(path: str, content: str) -> FileIndex:
    """Parse a Python source file with ast and extract top-level and class-level symbols."""
    raw = content.encode("utf-8", errors="ignore")
    total_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    fi = FileIndex(path=path, content_hash=_file_hash(raw), total_lines=total_lines)

    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        fi.parse_error = f"SyntaxError: {exc}"
        return fi

    # Collect imports (module names only, for context)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            fi.imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            fi.imports.append(node.module)

    # Collect top-level symbols and methods inside classes
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            fi.symbols.append(SymbolInfo(name=node.name, kind=kind, start_line=start, end_line=_node_end_line(node)))

        elif isinstance(node, ast.ClassDef):
            class_start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            fi.symbols.append(
                SymbolInfo(name=node.name, kind="class", start_line=class_start, end_line=_node_end_line(node))
            )
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sub_kind = "async_method" if isinstance(sub, ast.AsyncFunctionDef) else "method"
                    sub_start = sub.decorator_list[0].lineno if sub.decorator_list else sub.lineno
                    fi.symbols.append(
                        SymbolInfo(
                            name=sub.name,
                            kind=sub_kind,
                            start_line=sub_start,
                            end_line=_node_end_line(sub),
                            parent=node.name,
                        )
                    )

    return fi


def _index_plain_file(path: str, content: str) -> FileIndex:
    """Build a minimal FileIndex for non-Python files (no symbol extraction)."""
    raw = content.encode("utf-8", errors="ignore")
    total_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return FileIndex(path=path, content_hash=_file_hash(raw), total_lines=total_lines)


def build_index(repo_path: Path, file_paths: list[str], max_bytes: int = 64_000) -> CodeIndex:
    """Build a CodeIndex for the given file paths (relative to repo_path)."""
    files: dict[str, FileIndex] = {}
    for rel_path in file_paths:
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="ignore")[:max_bytes]
        except OSError:
            continue
        if rel_path.endswith(".py"):
            files[rel_path] = _index_python_file(rel_path, content)
        else:
            files[rel_path] = _index_plain_file(rel_path, content)
    return CodeIndex(repo_path=repo_path, files=files)
