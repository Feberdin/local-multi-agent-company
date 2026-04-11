"""
Purpose: Pydantic models for structured file-edit operations.
         Replaces the single 'create_or_update' action with targeted operations
         that avoid full file rewrites and reduce LLM token consumption.
Input/Output: The coding worker parses LLM JSON output into these models and hands them to patch_engine.
Important invariants: 'new_content' holds only the changed symbol or block, never the full file
                      unless action is 'create_or_update' or 'create_file'.
How to debug: If parsing fails, log the raw LLM JSON and compare against EDIT_ACTION_CHOICES.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class EditAction(StrEnum):
    REPLACE_SYMBOL_BODY = "replace_symbol_body"
    REPLACE_BLOCK = "replace_block"
    REPLACE_LINES = "replace_lines"
    INSERT_BEFORE_ANCHOR = "insert_before_anchor"
    INSERT_AFTER_ANCHOR = "insert_after_anchor"
    DELETE_BLOCK = "delete_block"
    CREATE_FILE = "create_file"
    DELETE_FILE = "delete_file"
    APPEND_TO_FILE = "append_to_file"
    CREATE_OR_UPDATE = "create_or_update"


#: Canonical action name list for inclusion in prompts and validation messages.
EDIT_ACTION_CHOICES = [a.value for a in EditAction]


class EditOperation(BaseModel):
    """One targeted file-edit operation applied by the patch engine."""

    action: EditAction
    file_path: str = Field(description="Relative path from repo root.")
    reason: str = Field(description="Why this change is needed.")
    new_content: str = Field(
        default="",
        description="Replacement content. For symbol/block ops: only the changed part. For create_or_update: full file.",
    )

    # Symbol-level operations (replace_symbol_body)
    symbol_name: str | None = Field(default=None, description="Function or class name for replace_symbol_body.")
    parent_symbol: str | None = Field(default=None, description="Class name when targeting a method.")

    # Anchor / block operations
    anchor_text: str | None = Field(
        default=None, description="Exact (or near-exact) text to locate the insertion or replacement point."
    )
    expected_old_content: str | None = Field(
        default=None, description="Old content for verification before replacement. Improves anchor precision."
    )

    # Line-range operations
    start_line: int | None = Field(default=None, description="1-indexed start line for replace_lines.")
    end_line: int | None = Field(default=None, description="1-indexed end line (inclusive) for replace_lines.")

    # Metadata
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    fallback_strategy: str | None = Field(
        default=None, description="Alternative action name to try if this operation fails."
    )


class EditPlan(BaseModel):
    """Complete set of edit operations produced by the coding LLM."""

    summary: str
    operations: list[EditOperation]


def normalize_raw_operation(raw: dict) -> dict:
    """
    Map legacy field names and fill defaults so raw LLM output can be parsed as EditOperation.

    Handles:
      - 'content' → 'new_content'  (legacy create_or_update format)
      - 'path'    → 'file_path'    (legacy short field name)
      - missing 'action' → 'create_or_update'
    """
    out = dict(raw)
    if "content" in out and "new_content" not in out:
        out["new_content"] = out.pop("content")
    if "path" in out and "file_path" not in out:
        out["file_path"] = out.pop("path")
    out.setdefault("action", EditAction.CREATE_OR_UPDATE.value)
    # Drop keys not in the model to avoid Pydantic extra-field errors
    valid = set(EditOperation.model_fields)
    return {k: v for k, v in out.items() if k in valid}
