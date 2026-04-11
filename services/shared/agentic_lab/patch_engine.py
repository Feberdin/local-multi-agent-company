"""
Purpose: Apply structured edit operations to files in a git repository.
         Supports symbol-level, anchor-based, line-based, and full-file edits.
         Tries each operation and reports per-operation results for precise error attribution.
Input/Output: Receives a list of EditOperation and a repo root path.
              Returns PatchResult describing success, errors, and strategies used per operation.
Important invariants:
  - Files are snapshotted before any writes; if an operation fails, previously written files are restored.
  - Python syntax is checked after each write; syntax errors become warnings, not hard failures.
  - No git staging or committing; callers run git diff afterwards to validate the working tree.
How to debug: Inspect PatchResult.operation_results for per-operation success/error/strategy detail.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass, field
from pathlib import Path

from services.shared.agentic_lab.edit_ops import EditAction, EditOperation


@dataclass
class OperationResult:
    """Outcome of applying a single edit operation."""

    operation_index: int
    action: str
    file_path: str
    success: bool
    strategy_used: str | None = None
    error: str | None = None
    lines_changed: int = 0
    syntax_warning: str | None = None


@dataclass
class PatchResult:
    """Aggregate result from applying a full list of edit operations."""

    success: bool
    operation_results: list[OperationResult] = field(default_factory=list)
    total_files_changed: int = 0
    errors: list[str] = field(default_factory=list)
    rollback_performed: bool = False

    @property
    def failed_operations(self) -> list[OperationResult]:
        return [r for r in self.operation_results if not r.success]

    def summary_text(self) -> str:
        ok = sum(1 for r in self.operation_results if r.success)
        total = len(self.operation_results)
        fail = total - ok
        parts = [f"{ok}/{total} operations applied, {self.total_files_changed} file(s) changed"]
        if fail:
            parts.append(f"{fail} failed")
        if self.rollback_performed:
            parts.append("rollback performed")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def apply_edit_plan(repo_path: Path, operations: list[EditOperation]) -> PatchResult:
    """
    Apply a list of edit operations with snapshot-based rollback on the first failure.

    Operations are applied in order. On failure, all previously written files are
    restored from their pre-apply snapshots. This keeps the working tree clean.
    """
    if not operations:
        return PatchResult(success=True)

    # Snapshot files that will be touched (to enable rollback)
    snapshot: dict[str, bytes | None] = {}
    for op in operations:
        full = repo_path / op.file_path
        if op.action not in {EditAction.CREATE_FILE, EditAction.DELETE_FILE}:
            try:
                snapshot[op.file_path] = full.read_bytes() if full.is_file() else None
            except OSError:
                snapshot[op.file_path] = None

    results: list[OperationResult] = []
    changed_paths: set[str] = set()

    for idx, op in enumerate(operations):
        result = _apply_single_operation(repo_path, op, idx)
        results.append(result)
        if result.success:
            changed_paths.add(op.file_path)
        else:
            _rollback(repo_path, snapshot)
            all_errors = [r.error for r in results if r.error]
            return PatchResult(
                success=False,
                operation_results=results,
                total_files_changed=0,
                errors=all_errors,
                rollback_performed=True,
            )

    syntax_warnings = [r.syntax_warning for r in results if r.syntax_warning]
    return PatchResult(
        success=True,
        operation_results=results,
        total_files_changed=len(changed_paths),
        errors=syntax_warnings,  # syntax issues are surfaced as non-fatal errors
    )


# ---------------------------------------------------------------------------
# Operation dispatcher
# ---------------------------------------------------------------------------


def _apply_single_operation(repo_path: Path, op: EditOperation, idx: int) -> OperationResult:
    result = OperationResult(
        operation_index=idx,
        action=op.action.value,
        file_path=op.file_path,
        success=False,
    )
    rel = Path(op.file_path)
    full_path = (repo_path / rel).resolve()

    # Safety: reject absolute paths, path traversal, or out-of-repo writes
    if rel.is_absolute() or ".." in rel.parts:
        result.error = f"Rejected unsafe path: {op.file_path}"
        return result
    if repo_path.resolve() not in full_path.parents and full_path != repo_path.resolve():
        result.error = f"Rejected out-of-repo path: {op.file_path}"
        return result

    try:
        if op.action == EditAction.CREATE_FILE:
            return _do_create_file(full_path, op, result)
        if op.action == EditAction.DELETE_FILE:
            return _do_delete_file(full_path, op, result)
        if op.action == EditAction.APPEND_TO_FILE:
            return _do_append(full_path, op, result)
        if op.action == EditAction.CREATE_OR_UPDATE:
            return _do_create_or_update(full_path, op, result)

        # All remaining operations require the file to already exist
        if not full_path.is_file():
            result.error = f"File not found: {op.file_path}"
            return result

        content = full_path.read_text(encoding="utf-8", errors="ignore")
        old_line_count = content.count("\n")

        if op.action == EditAction.REPLACE_SYMBOL_BODY:
            new_content, strategy = _apply_replace_symbol(content, op)
        elif op.action == EditAction.REPLACE_BLOCK:
            new_content, strategy = _apply_replace_block(content, op)
        elif op.action == EditAction.REPLACE_LINES:
            new_content, strategy = _apply_replace_lines(content, op)
        elif op.action == EditAction.INSERT_BEFORE_ANCHOR:
            new_content, strategy = _apply_insert_anchor(content, op, before=True)
        elif op.action == EditAction.INSERT_AFTER_ANCHOR:
            new_content, strategy = _apply_insert_anchor(content, op, before=False)
        elif op.action == EditAction.DELETE_BLOCK:
            new_content, strategy = _apply_delete_block(content, op)
        else:
            result.error = f"Unhandled action: {op.action}"
            return result

        full_path.write_text(new_content, encoding="utf-8")
        result.success = True
        result.strategy_used = strategy
        result.lines_changed = abs(new_content.count("\n") - old_line_count)
        if op.file_path.endswith(".py"):
            result.syntax_warning = _syntax_check(new_content, op.file_path)

    except Exception as exc:  # pragma: no cover - defensive for unexpected runtime errors
        result.error = f"{type(exc).__name__}: {exc}"

    return result


# ---------------------------------------------------------------------------
# Simple file operations
# ---------------------------------------------------------------------------


def _do_create_file(full_path: Path, op: EditOperation, result: OperationResult) -> OperationResult:
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(op.new_content, encoding="utf-8")
    result.success = True
    result.strategy_used = "create_file"
    if op.file_path.endswith(".py"):
        result.syntax_warning = _syntax_check(op.new_content, op.file_path)
    return result


def _do_delete_file(full_path: Path, op: EditOperation, result: OperationResult) -> OperationResult:
    if not full_path.is_file():
        result.error = f"File to delete not found: {op.file_path}"
        return result
    full_path.unlink()
    result.success = True
    result.strategy_used = "delete_file"
    return result


def _do_append(full_path: Path, op: EditOperation, result: OperationResult) -> OperationResult:
    if not full_path.is_file():
        result.error = f"File to append to not found: {op.file_path}"
        return result
    existing = full_path.read_text(encoding="utf-8", errors="ignore")
    separator = "" if existing.endswith("\n") else "\n"
    full_path.write_text(existing + separator + op.new_content, encoding="utf-8")
    result.success = True
    result.strategy_used = "append_to_file"
    return result


def _do_create_or_update(full_path: Path, op: EditOperation, result: OperationResult) -> OperationResult:
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(op.new_content, encoding="utf-8")
    result.success = True
    result.strategy_used = "create_or_update"
    if op.file_path.endswith(".py"):
        result.syntax_warning = _syntax_check(op.new_content, op.file_path)
    return result


# ---------------------------------------------------------------------------
# Targeted edit operations
# ---------------------------------------------------------------------------


def _apply_replace_symbol(content: str, op: EditOperation) -> tuple[str, str]:
    """
    Replace a function or class definition (including decorators) by name using AST.

    Searches top-level nodes first, then one level deep inside classes (for methods).
    The entire node span (decorator_list start → end_lineno) is replaced with new_content.
    """
    if not op.symbol_name:
        raise ValueError("replace_symbol_body requires symbol_name")

    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        raise ValueError(f"Cannot parse file — syntax error before edit: {exc}") from exc

    lines = content.splitlines(keepends=True)

    def _search(nodes: list[ast.stmt], class_name: str | None = None) -> str | None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name_match = node.name == op.symbol_name
                parent_match = op.parent_symbol is None or class_name == op.parent_symbol
                if name_match and parent_match:
                    start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
                    end = node.end_lineno  # 1-indexed inclusive → 0-indexed exclusive for slicing
                    new_block = op.new_content if op.new_content.endswith("\n") else op.new_content + "\n"
                    return "".join(lines[:start]) + new_block + "".join(lines[end:])
                if isinstance(node, ast.ClassDef):
                    result = _search(node.body, class_name=node.name)
                    if result is not None:
                        return result
        return None

    replaced = _search(tree.body)
    if replaced is None:
        parent_hint = f" in class {op.parent_symbol}" if op.parent_symbol else ""
        raise ValueError(f"Symbol '{op.symbol_name}'{parent_hint} not found in file")
    return replaced, "replace_symbol_body"


def _apply_replace_block(content: str, op: EditOperation) -> tuple[str, str]:
    """
    Replace a block of lines identified by anchor text.

    The number of lines replaced equals len(expected_old_content) if provided,
    otherwise len(anchor_text). The anchor is located with fuzzy matching.
    """
    if not op.anchor_text:
        raise ValueError("replace_block requires anchor_text")

    lines = content.splitlines(keepends=True)
    anchor_lines = op.anchor_text.splitlines(keepends=False)
    replace_count = (
        len(op.expected_old_content.splitlines()) if op.expected_old_content else len(anchor_lines)
    )

    idx = _fuzzy_find_anchor(lines, anchor_lines)
    if idx is None:
        raise ValueError(f"Anchor text not found (fuzzy threshold 0.75): {op.anchor_text[:80]!r}")

    new_block = op.new_content if not op.new_content or op.new_content.endswith("\n") else op.new_content + "\n"
    new_lines = lines[:idx] + ([new_block] if new_block else []) + lines[idx + replace_count :]
    return "".join(new_lines), "replace_block"


def _apply_replace_lines(content: str, op: EditOperation) -> tuple[str, str]:
    """Replace a line range (1-indexed, both ends inclusive)."""
    if op.start_line is None or op.end_line is None:
        raise ValueError("replace_lines requires start_line and end_line")

    lines = content.splitlines(keepends=True)
    total = len(lines)
    start = max(0, op.start_line - 1)
    end = min(total, op.end_line)

    if start >= total or start > end:
        raise ValueError(
            f"Line range {op.start_line}-{op.end_line} is out of bounds (file has {total} lines)"
        )

    new_block = op.new_content if not op.new_content or op.new_content.endswith("\n") else op.new_content + "\n"
    new_lines = lines[:start] + ([new_block] if new_block else []) + lines[end:]
    return "".join(new_lines), "replace_lines"


def _apply_insert_anchor(content: str, op: EditOperation, *, before: bool) -> tuple[str, str]:
    """Insert new_content immediately before or after the matched anchor line(s)."""
    if not op.anchor_text:
        raise ValueError("insert_before/after_anchor requires anchor_text")

    lines = content.splitlines(keepends=True)
    anchor_lines = op.anchor_text.splitlines(keepends=False)
    idx = _fuzzy_find_anchor(lines, anchor_lines)
    if idx is None:
        raise ValueError(f"Anchor text not found: {op.anchor_text[:80]!r}")

    new_block = op.new_content if not op.new_content or op.new_content.endswith("\n") else op.new_content + "\n"
    if before:
        new_lines = lines[:idx] + [new_block] + lines[idx:]
        strategy = "insert_before_anchor"
    else:
        insert_at = idx + len(anchor_lines)
        new_lines = lines[:insert_at] + [new_block] + lines[insert_at:]
        strategy = "insert_after_anchor"

    return "".join(new_lines), strategy


def _apply_delete_block(content: str, op: EditOperation) -> tuple[str, str]:
    """Delete a block of lines identified by anchor text."""
    if not op.anchor_text:
        raise ValueError("delete_block requires anchor_text")

    lines = content.splitlines(keepends=True)
    anchor_lines = op.anchor_text.splitlines(keepends=False)
    delete_count = (
        len(op.expected_old_content.splitlines()) if op.expected_old_content else len(anchor_lines)
    )

    idx = _fuzzy_find_anchor(lines, anchor_lines)
    if idx is None:
        raise ValueError(f"Anchor text not found for deletion: {op.anchor_text[:80]!r}")

    new_lines = lines[:idx] + lines[idx + delete_count :]
    return "".join(new_lines), "delete_block"


# ---------------------------------------------------------------------------
# Fuzzy anchor matching
# ---------------------------------------------------------------------------


def _fuzzy_find_anchor(
    content_lines: list[str],
    anchor_lines: list[str],
    *,
    threshold: float = 0.75,
) -> int | None:
    """
    Locate anchor_lines within content_lines in three passes:
      1. Exact match (with trailing newlines stripped)
      2. Stripped exact match (leading/trailing whitespace removed)
      3. Fuzzy match via difflib.SequenceMatcher (ratio >= threshold)

    Returns the 0-indexed line number of the anchor start, or None if not found.
    """
    n = len(anchor_lines)
    m = len(content_lines)
    if n == 0 or m < n:
        return None

    raw_anchor = [line.rstrip("\n") for line in anchor_lines]
    stripped_anchor = [line.strip() for line in anchor_lines]

    # Pass 1: exact (strip trailing newline only)
    for i in range(m - n + 1):
        chunk = [line.rstrip("\n") for line in content_lines[i : i + n]]
        if chunk == raw_anchor:
            return i

    # Pass 2: stripped exact
    for i in range(m - n + 1):
        chunk = [line.strip() for line in content_lines[i : i + n]]
        if chunk == stripped_anchor:
            return i

    # Pass 3: fuzzy over joined text
    anchor_joined = "\n".join(stripped_anchor)
    best_ratio = 0.0
    best_idx = -1
    for i in range(m - n + 1):
        chunk_joined = "\n".join(line.strip() for line in content_lines[i : i + n])
        ratio = difflib.SequenceMatcher(None, anchor_joined, chunk_joined).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i

    return best_idx if best_ratio >= threshold else None


# ---------------------------------------------------------------------------
# Syntax check
# ---------------------------------------------------------------------------


def _syntax_check(content: str, file_path: str) -> str | None:
    """Return a human-readable error if the Python content fails ast.parse, else None."""
    try:
        ast.parse(content)
        return None
    except SyntaxError as exc:
        return f"Syntax error in {file_path} after edit: {exc.msg} at line {exc.lineno}"


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def _rollback(repo_path: Path, snapshot: dict[str, bytes | None]) -> None:
    """Restore snapshotted file bytes. Best-effort; OSError is silently suppressed."""
    for rel_path, original in snapshot.items():
        full = repo_path / rel_path
        try:
            if original is None:
                if full.exists():
                    full.unlink()
            else:
                full.write_bytes(original)
        except OSError:
            pass
