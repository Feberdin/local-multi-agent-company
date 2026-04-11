"""
Unit tests for services/shared/agentic_lab/patch_engine.py.
Covers all edit actions, rollback, fuzzy anchor matching, and syntax checking.
"""

from __future__ import annotations

from pathlib import Path

from services.shared.agentic_lab.edit_ops import EditAction, EditOperation
from services.shared.agentic_lab.patch_engine import (
    _fuzzy_find_anchor,
    _syntax_check,
    apply_edit_plan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_PY = """\
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

class Calculator:
    def multiply(self, a, b):
        return a * b
"""


def _op(**kwargs) -> EditOperation:
    defaults = {"action": EditAction.CREATE_OR_UPDATE, "file_path": "target.py", "reason": "test"}
    defaults.update(kwargs)
    return EditOperation(**defaults)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# create_file
# ---------------------------------------------------------------------------


def test_create_file_creates_new_file(tmp_path):
    op = _op(action=EditAction.CREATE_FILE, file_path="new_dir/new.py", new_content="x = 1\n")
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    assert (tmp_path / "new_dir" / "new.py").read_text() == "x = 1\n"


def test_create_file_overwrites_existing(tmp_path):
    _write(tmp_path, "existing.py", "old content\n")
    op = _op(action=EditAction.CREATE_FILE, file_path="existing.py", new_content="new content\n")
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    assert (tmp_path / "existing.py").read_text() == "new content\n"


# ---------------------------------------------------------------------------
# create_or_update
# ---------------------------------------------------------------------------


def test_create_or_update_full_rewrite(tmp_path):
    _write(tmp_path, "target.py", "old\n")
    op = _op(action=EditAction.CREATE_OR_UPDATE, file_path="target.py", new_content="new content\n")
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    assert (tmp_path / "target.py").read_text() == "new content\n"


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


def test_delete_file_removes_file(tmp_path):
    _write(tmp_path, "to_delete.py", "content\n")
    op = _op(action=EditAction.DELETE_FILE, file_path="to_delete.py")
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    assert not (tmp_path / "to_delete.py").exists()


def test_delete_file_error_if_missing(tmp_path):
    op = _op(action=EditAction.DELETE_FILE, file_path="nonexistent.py")
    result = apply_edit_plan(tmp_path, [op])
    assert not result.success
    assert result.failed_operations[0].error is not None


# ---------------------------------------------------------------------------
# append_to_file
# ---------------------------------------------------------------------------


def test_append_to_file_adds_content(tmp_path):
    _write(tmp_path, "t.py", "line1\n")
    op = _op(action=EditAction.APPEND_TO_FILE, file_path="t.py", new_content="line2\n")
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    assert (tmp_path / "t.py").read_text() == "line1\nline2\n"


def test_append_adds_separator_if_no_trailing_newline(tmp_path):
    _write(tmp_path, "t.py", "line1")
    op = _op(action=EditAction.APPEND_TO_FILE, file_path="t.py", new_content="line2\n")
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    text = (tmp_path / "t.py").read_text()
    assert "line1\nline2" in text


# ---------------------------------------------------------------------------
# replace_symbol_body
# ---------------------------------------------------------------------------


def test_replace_symbol_body_replaces_function(tmp_path):
    _write(tmp_path, "calc.py", _SAMPLE_PY)
    new_func = "def add(a, b):\n    return a + b + 1\n"
    op = _op(
        action=EditAction.REPLACE_SYMBOL_BODY,
        file_path="calc.py",
        symbol_name="add",
        new_content=new_func,
    )
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    content = (tmp_path / "calc.py").read_text()
    assert "return a + b + 1" in content
    assert "return a + b\n" not in content


def test_replace_symbol_body_replaces_method(tmp_path):
    _write(tmp_path, "calc.py", _SAMPLE_PY)
    new_method = "    def multiply(self, a, b):\n        return a * b * 2\n"
    op = _op(
        action=EditAction.REPLACE_SYMBOL_BODY,
        file_path="calc.py",
        symbol_name="multiply",
        parent_symbol="Calculator",
        new_content=new_method,
    )
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    content = (tmp_path / "calc.py").read_text()
    assert "a * b * 2" in content


def test_replace_symbol_body_error_if_symbol_not_found(tmp_path):
    _write(tmp_path, "calc.py", _SAMPLE_PY)
    op = _op(
        action=EditAction.REPLACE_SYMBOL_BODY,
        file_path="calc.py",
        symbol_name="nonexistent",
        new_content="def nonexistent(): pass\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert not result.success


def test_replace_symbol_body_error_if_no_symbol_name(tmp_path):
    _write(tmp_path, "calc.py", _SAMPLE_PY)
    op = _op(
        action=EditAction.REPLACE_SYMBOL_BODY,
        file_path="calc.py",
        symbol_name=None,
        new_content="def x(): pass\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert not result.success


# ---------------------------------------------------------------------------
# replace_block
# ---------------------------------------------------------------------------


def test_replace_block_exact_anchor(tmp_path):
    _write(tmp_path, "t.py", "line1\nline2\nline3\n")
    op = _op(
        action=EditAction.REPLACE_BLOCK,
        file_path="t.py",
        anchor_text="line2",
        new_content="replaced\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    assert "replaced" in (tmp_path / "t.py").read_text()
    assert "line2" not in (tmp_path / "t.py").read_text()


def test_replace_block_fuzzy_anchor(tmp_path):
    _write(tmp_path, "t.py", "    line2  \nline3\n")
    op = _op(
        action=EditAction.REPLACE_BLOCK,
        file_path="t.py",
        anchor_text="line2",
        new_content="replaced\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert result.success


def test_replace_block_anchor_not_found(tmp_path):
    _write(tmp_path, "t.py", "line1\nline2\n")
    op = _op(
        action=EditAction.REPLACE_BLOCK,
        file_path="t.py",
        anchor_text="XXXXXX_does_not_exist",
        new_content="replaced\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert not result.success


# ---------------------------------------------------------------------------
# replace_lines
# ---------------------------------------------------------------------------


def test_replace_lines_replaces_range(tmp_path):
    _write(tmp_path, "t.py", "line1\nline2\nline3\nline4\n")
    op = _op(
        action=EditAction.REPLACE_LINES,
        file_path="t.py",
        start_line=2,
        end_line=3,
        new_content="replaced\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    content = (tmp_path / "t.py").read_text()
    assert "replaced" in content
    assert "line2" not in content
    assert "line3" not in content
    assert "line1" in content
    assert "line4" in content


def test_replace_lines_out_of_bounds(tmp_path):
    _write(tmp_path, "t.py", "line1\n")
    op = _op(
        action=EditAction.REPLACE_LINES,
        file_path="t.py",
        start_line=100,
        end_line=200,
        new_content="x\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert not result.success


# ---------------------------------------------------------------------------
# insert_before_anchor / insert_after_anchor
# ---------------------------------------------------------------------------


def test_insert_before_anchor(tmp_path):
    _write(tmp_path, "t.py", "line1\nline2\nline3\n")
    op = _op(
        action=EditAction.INSERT_BEFORE_ANCHOR,
        file_path="t.py",
        anchor_text="line2",
        new_content="inserted\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    lines = (tmp_path / "t.py").read_text().splitlines()
    inserted_idx = lines.index("inserted")
    line2_idx = lines.index("line2")
    assert inserted_idx == line2_idx - 1


def test_insert_after_anchor(tmp_path):
    _write(tmp_path, "t.py", "line1\nline2\nline3\n")
    op = _op(
        action=EditAction.INSERT_AFTER_ANCHOR,
        file_path="t.py",
        anchor_text="line2",
        new_content="inserted\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    lines = (tmp_path / "t.py").read_text().splitlines()
    line2_idx = lines.index("line2")
    inserted_idx = lines.index("inserted")
    assert inserted_idx == line2_idx + 1


# ---------------------------------------------------------------------------
# delete_block
# ---------------------------------------------------------------------------


def test_delete_block_removes_lines(tmp_path):
    _write(tmp_path, "t.py", "line1\nline2\nline3\n")
    op = _op(
        action=EditAction.DELETE_BLOCK,
        file_path="t.py",
        anchor_text="line2",
    )
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    assert "line2" not in (tmp_path / "t.py").read_text()
    assert "line1" in (tmp_path / "t.py").read_text()


# ---------------------------------------------------------------------------
# Rollback on failure
# ---------------------------------------------------------------------------


def test_rollback_restores_first_file_after_second_op_fails(tmp_path):
    _write(tmp_path, "a.py", "original_a\n")
    _write(tmp_path, "b.py", "original_b\n")
    ops = [
        _op(action=EditAction.CREATE_OR_UPDATE, file_path="a.py", new_content="modified_a\n"),
        _op(
            action=EditAction.REPLACE_SYMBOL_BODY,
            file_path="b.py",
            symbol_name="no_such_func",
            new_content="def no_such_func(): pass\n",
        ),
    ]
    result = apply_edit_plan(tmp_path, ops)
    assert not result.success
    assert result.rollback_performed
    # a.py should be restored to original
    assert (tmp_path / "a.py").read_text() == "original_a\n"


def test_empty_operation_list_returns_success(tmp_path):
    result = apply_edit_plan(tmp_path, [])
    assert result.success
    assert result.operation_results == []


# ---------------------------------------------------------------------------
# Safety: path traversal / absolute path rejection
# ---------------------------------------------------------------------------


def test_absolute_path_rejected(tmp_path):
    op = _op(action=EditAction.CREATE_OR_UPDATE, file_path="/etc/passwd", new_content="evil\n")
    result = apply_edit_plan(tmp_path, [op])
    assert not result.success
    assert "Rejected" in result.failed_operations[0].error


def test_path_traversal_rejected(tmp_path):
    op = _op(action=EditAction.CREATE_OR_UPDATE, file_path="../outside.py", new_content="evil\n")
    result = apply_edit_plan(tmp_path, [op])
    assert not result.success
    assert "Rejected" in result.failed_operations[0].error


# ---------------------------------------------------------------------------
# Syntax check integration
# ---------------------------------------------------------------------------


def test_syntax_warning_set_for_broken_python(tmp_path):
    _write(tmp_path, "broken.py", "def ok(): pass\n")
    op = _op(
        action=EditAction.CREATE_OR_UPDATE,
        file_path="broken.py",
        new_content="def broken(:\n    pass\n",
    )
    result = apply_edit_plan(tmp_path, [op])
    # Operation succeeds (syntax errors are warnings, not hard failures)
    assert result.success
    assert any(r.syntax_warning for r in result.operation_results)


def test_no_syntax_warning_for_valid_python(tmp_path):
    _write(tmp_path, "ok.py", "x = 1\n")
    op = _op(action=EditAction.CREATE_OR_UPDATE, file_path="ok.py", new_content="x = 2\n")
    result = apply_edit_plan(tmp_path, [op])
    assert result.success
    assert all(r.syntax_warning is None for r in result.operation_results)


# ---------------------------------------------------------------------------
# _fuzzy_find_anchor
# ---------------------------------------------------------------------------


def test_fuzzy_find_exact_match():
    lines = ["line1\n", "line2\n", "line3\n"]
    assert _fuzzy_find_anchor(lines, ["line2"]) == 1


def test_fuzzy_find_stripped_match():
    lines = ["  line2  \n", "line3\n"]
    assert _fuzzy_find_anchor(lines, ["line2"]) == 0


def test_fuzzy_find_no_match_below_threshold():
    lines = ["completely different content\n"]
    assert _fuzzy_find_anchor(lines, ["nothing matching"], threshold=0.99) is None


def test_fuzzy_find_multiline_anchor():
    lines = ["a\n", "b\n", "c\n", "d\n"]
    assert _fuzzy_find_anchor(lines, ["b", "c"]) == 1


def test_fuzzy_find_empty_anchor_returns_none():
    lines = ["line1\n"]
    assert _fuzzy_find_anchor(lines, []) is None


# ---------------------------------------------------------------------------
# _syntax_check
# ---------------------------------------------------------------------------


def test_syntax_check_valid_returns_none():
    assert _syntax_check("x = 1\n", "test.py") is None


def test_syntax_check_invalid_returns_message():
    msg = _syntax_check("def broken(:\n    pass\n", "test.py")
    assert msg is not None
    assert "test.py" in msg


# ---------------------------------------------------------------------------
# PatchResult.summary_text
# ---------------------------------------------------------------------------


def test_summary_text_all_success(tmp_path):
    _write(tmp_path, "t.py", "x = 1\n")
    result = apply_edit_plan(
        tmp_path,
        [_op(action=EditAction.CREATE_OR_UPDATE, file_path="t.py", new_content="x = 2\n")],
    )
    text = result.summary_text()
    assert "1/1" in text


def test_summary_text_includes_rollback(tmp_path):
    _write(tmp_path, "t.py", "x = 1\n")
    ops = [
        _op(action=EditAction.CREATE_OR_UPDATE, file_path="t.py", new_content="x = 2\n"),
        _op(
            action=EditAction.REPLACE_SYMBOL_BODY,
            file_path="t.py",
            symbol_name="no_sym",
            new_content="def no_sym(): pass\n",
        ),
    ]
    result = apply_edit_plan(tmp_path, ops)
    assert result.rollback_performed
    assert "rollback" in result.summary_text()
