"""
Unit tests for services/shared/agentic_lab/code_index.py.
Covers symbol extraction, import collection, non-Python files, format_for_prompt, and build_index.
"""

from __future__ import annotations

from services.shared.agentic_lab.code_index import (
    CodeIndex,
    build_index,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_PY = """\
import os
from pathlib import Path

def top_func(x):
    return x + 1

async def async_func():
    pass

class MyClass:
    def method_one(self):
        pass

    async def async_method(self):
        pass
"""

_DECORATED_PY = """\
import functools

def decorator(fn):
    return fn

@decorator
def decorated_func():
    pass

class Outer:
    @staticmethod
    def static_method():
        pass
"""

_SYNTAX_ERROR_PY = "def broken(:\n    pass\n"


# ---------------------------------------------------------------------------
# _index_python_file (via build_index on a tmp dir)
# ---------------------------------------------------------------------------


def test_top_level_function_extracted(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    fi = idx.get_file("a.py")
    assert fi is not None
    sym = idx.get_symbol("a.py", "top_func")
    assert sym is not None
    assert sym.kind == "function"
    assert sym.parent is None


def test_async_function_extracted(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    sym = idx.get_symbol("a.py", "async_func")
    assert sym is not None
    assert sym.kind == "async_function"


def test_class_extracted(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    sym = idx.get_symbol("a.py", "MyClass")
    assert sym is not None
    assert sym.kind == "class"


def test_method_extracted_with_parent(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    sym = idx.get_symbol("a.py", "method_one")
    assert sym is not None
    assert sym.kind == "method"
    assert sym.parent == "MyClass"


def test_async_method_extracted(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    sym = idx.get_symbol("a.py", "async_method")
    assert sym is not None
    assert sym.kind == "async_method"
    assert sym.parent == "MyClass"


def test_decorator_line_included_in_start(tmp_path):
    (tmp_path / "a.py").write_text(_DECORATED_PY)
    idx = build_index(tmp_path, ["a.py"])
    sym = idx.get_symbol("a.py", "decorated_func")
    assert sym is not None
    # decorator is on line 6, function def on line 7
    assert sym.start_line == 6


def test_static_method_in_class(tmp_path):
    (tmp_path / "a.py").write_text(_DECORATED_PY)
    idx = build_index(tmp_path, ["a.py"])
    sym = idx.get_symbol("a.py", "static_method")
    assert sym is not None
    assert sym.parent == "Outer"


def test_line_numbers_are_one_indexed(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    sym = idx.get_symbol("a.py", "top_func")
    assert sym.start_line >= 1
    assert sym.end_line >= sym.start_line


def test_imports_collected(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    fi = idx.get_file("a.py")
    assert "os" in fi.imports
    assert "pathlib" in fi.imports


def test_syntax_error_recorded(tmp_path):
    (tmp_path / "bad.py").write_text(_SYNTAX_ERROR_PY)
    idx = build_index(tmp_path, ["bad.py"])
    fi = idx.get_file("bad.py")
    assert fi is not None
    assert fi.parse_error is not None
    assert "SyntaxError" in fi.parse_error


def test_non_python_file_indexed_no_symbols(tmp_path):
    (tmp_path / "config.yaml").write_text("key: value\n")
    idx = build_index(tmp_path, ["config.yaml"])
    fi = idx.get_file("config.yaml")
    assert fi is not None
    assert fi.symbols == []
    assert fi.parse_error is None


def test_missing_file_skipped(tmp_path):
    idx = build_index(tmp_path, ["nonexistent.py"])
    assert idx.get_file("nonexistent.py") is None


def test_get_symbol_content_returns_source(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    content = idx.get_symbol_content("a.py", "top_func")
    assert content is not None
    assert "def top_func" in content
    assert "return x + 1" in content


def test_get_symbol_returns_none_for_unknown(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    assert idx.get_symbol("a.py", "no_such_symbol") is None


def test_get_symbol_returns_none_for_unknown_file(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    assert idx.get_symbol("other.py", "top_func") is None


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------


def test_format_for_prompt_empty_index(tmp_path):
    idx = CodeIndex(tmp_path, {})
    assert idx.format_for_prompt() == ""


def test_format_for_prompt_contains_symbol(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    block = idx.format_for_prompt()
    assert "top_func" in block
    assert "MyClass" in block
    assert "method_one" in block


def test_format_for_prompt_shows_line_range(tmp_path):
    (tmp_path / "a.py").write_text(_SIMPLE_PY)
    idx = build_index(tmp_path, ["a.py"])
    block = idx.format_for_prompt()
    assert "lines" in block


def test_format_for_prompt_parse_error_noted(tmp_path):
    (tmp_path / "bad.py").write_text(_SYNTAX_ERROR_PY)
    idx = build_index(tmp_path, ["bad.py"])
    block = idx.format_for_prompt()
    assert "not parseable" in block


def test_format_for_prompt_respects_max_symbols(tmp_path):
    # Generate a file with many functions
    lines = [f"def func_{i}(): pass\n" for i in range(50)]
    (tmp_path / "many.py").write_text("".join(lines))
    idx = build_index(tmp_path, ["many.py"])
    block = idx.format_for_prompt(max_symbols_per_file=5)
    assert "and 45 more" in block


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


def test_content_hash_differs_for_different_content(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("x = 2\n")
    idx = build_index(tmp_path, ["a.py", "b.py"])
    assert idx.get_file("a.py").content_hash != idx.get_file("b.py").content_hash


def test_content_hash_same_for_identical_content(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("x = 1\n")
    idx = build_index(tmp_path, ["a.py", "b.py"])
    assert idx.get_file("a.py").content_hash == idx.get_file("b.py").content_hash
