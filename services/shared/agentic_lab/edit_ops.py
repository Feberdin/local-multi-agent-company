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

import re
import unicodedata
from enum import StrEnum
from typing import Any

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


# Why this exists:
# The LLM client validates edit plans before the coding worker starts parsing and
# applying file operations. This keeps malformed edit plans from failing late in
# the patch engine after a long worker run.
#
# What happens here:
# We define the minimum fields each operation type must provide so the shared
# JSON-contract validator can reject semantically incomplete plans early.
_ACTION_REQUIRED_FIELDS: dict[EditAction, tuple[str, ...]] = {
    EditAction.REPLACE_SYMBOL_BODY: ("file_path", "reason", "symbol_name", "new_content"),
    EditAction.REPLACE_BLOCK: ("file_path", "reason", "anchor_text", "new_content"),
    EditAction.REPLACE_LINES: ("file_path", "reason", "start_line", "end_line", "new_content"),
    EditAction.INSERT_BEFORE_ANCHOR: ("file_path", "reason", "anchor_text", "new_content"),
    EditAction.INSERT_AFTER_ANCHOR: ("file_path", "reason", "anchor_text", "new_content"),
    EditAction.DELETE_BLOCK: ("file_path", "reason", "anchor_text"),
    EditAction.CREATE_FILE: ("file_path", "reason", "new_content"),
    EditAction.DELETE_FILE: ("file_path", "reason"),
    EditAction.APPEND_TO_FILE: ("file_path", "reason", "new_content"),
    EditAction.CREATE_OR_UPDATE: ("file_path", "reason", "new_content"),
}

# Why this exists:
# Local edit-plan models sometimes return a syntactically valid JSON object with
# `operations: []`, but the textual summary is just a generic assistant no-op
# such as "No specific code change requested" or the German equivalents seen in
# real Unraid runs. Those replies must count as invalid so the shared LLM client
# can trigger its stricter repair pass and then the provider fallback.
#
# Example input:
#   {"summary":"Keine Dateiänderungen erforderlich: Es wurden keine spezifischen Änderungen bereitgestellt.","operations":[]}
# Example output:
#   validation error string that causes `complete_json()` to fall back to the
#   configured secondary model instead of accepting the empty plan.
_GENERIC_EMPTY_PLAN_PATTERNS = (
    "no specific code change",
    "no code change requested",
    "no specific operation requested",
    "no target file",
    "no target files",
    "no file path",
    "without a specific file path",
    "without a specific filepath",
    "without a clear requirement",
    "no specific filepath",
    "seeking assistance with specific aspects",
    "requested analysis or implementation help for specific aspects",
    "shared a comprehensive setup",
    "how can i help",
    "keine dateianderungen erforderlich",
    "keine dateianderung erforderlich",
    "keine spezifischen anderungen",
    "keine spezifische anderung",
    "keine spezifischen anforderungen",
    "keine konkrete codeanderung",
    "keine konkreten codeanderungen",
    "kein konkreter codeanderungsauftrag",
    "kein konkreter code-anderungsauftrag",
    "keine code-operationen moglich",
    "keine datei-operationen moglich",
    "keine ziel datei",
    "keine zieldatei",
    "keine ziel-datei",
    "keine ziel dateien",
    "keine ziel-dateien",
    "kein dateipfad",
    "keine ziel datei angegeben",
    "keine klare anforderung",
    "keine bearbeitung erfordern",
    "keine bearbeitung erforderlich",
)


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

    # Why this exists:
    # Local models often return "almost right" edit operations that use field
    # aliases (`file`, `description`, `new_code`) or nest the target location in
    # one `location` object. Normalizing those shapes here keeps the shared
    # contract validator and the coding worker aligned, so a nearly-correct
    # fallback reply becomes one real edit instead of another full worker retry.
    if "content" in out and "new_content" not in out:
        out["new_content"] = out.pop("content")
    for alias in ("new_code", "replacement", "replacement_text", "code", "body", "text"):
        if alias in out and "new_content" not in out and isinstance(out.get(alias), str):
            out["new_content"] = out.pop(alias)
            break

    if "path" in out and "file_path" not in out:
        out["file_path"] = out.pop("path")
    for alias in ("file", "filepath", "target_file"):
        if alias in out and "file_path" not in out and isinstance(out.get(alias), str):
            out["file_path"] = out.pop(alias)
            break

    for alias in ("description", "details", "explanation", "why"):
        if alias in out and "reason" not in out and isinstance(out.get(alias), str):
            out["reason"] = out.pop(alias)
            break

    _apply_location_aliases(out)
    _normalize_action_aliases(out)
    out.setdefault("action", _infer_operation_action(out))
    # Drop keys not in the model to avoid Pydantic extra-field errors
    valid = set(EditOperation.model_fields)
    return {k: v for k, v in out.items() if k in valid}


def normalize_edit_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize near-valid edit-plan payloads into the canonical shared contract.

    Example:
      Input:
        {
          "summary": "Add clone error handling",
          "operations": [
            {
              "file": "services/shared/agentic_lab/repo_tools.py",
              "changes": [
                {
                  "location": {"type": "function", "name": "_clone_target_from_best_source"},
                  "new_code": "def _clone_target_from_best_source(...):\\n    ...",
                  "description": "Wrap the clone call with clearer error handling."
                }
              ]
            }
          ]
        }
      Output:
        {
          "summary": "Add clone error handling",
          "operations": [
            {
              "action": "replace_symbol_body",
              "file_path": "services/shared/agentic_lab/repo_tools.py",
              "symbol_name": "_clone_target_from_best_source",
              "reason": "Wrap the clone call with clearer error handling.",
              "new_content": "def _clone_target_from_best_source(...):\\n    ..."
            }
          ]
        }
    """

    normalized = dict(payload)

    if "summary" not in normalized or not isinstance(normalized.get("summary"), str) or not normalized["summary"].strip():
        for alias in ("plan_summary", "title", "description"):
            candidate = normalized.get(alias)
            if isinstance(candidate, str) and candidate.strip():
                normalized["summary"] = candidate.strip()
                break

    operations = normalized.get("operations")
    if not isinstance(operations, list) and isinstance(normalized.get("changes"), list):
        operations = normalized.get("changes")

    if isinstance(operations, list):
        normalized["operations"] = expand_raw_operations(operations)

    return normalized


def expand_raw_operations(raw_operations: list[Any]) -> list[dict[str, Any]]:
    """Flatten nested operation containers into the canonical flat edit-plan list."""

    expanded: list[dict[str, Any]] = []
    for raw in raw_operations:
        expanded.extend(_expand_one_raw_operation(raw))
    return expanded


def validate_raw_operation(raw: Any, *, index: int | None = None) -> str | None:
    """
    Return a human-readable validation error for malformed raw LLM edit operations.

    Example:
      Input: {"action": "create_or_update"}
      Output: "Operation 0 for action `create_or_update` is missing required fields: file_path, reason, new_content."
    """

    operation_label = f"Operation {index}" if index is not None else "Operation"
    if not isinstance(raw, dict):
        return f"{operation_label} must be a JSON object, got `{type(raw).__name__}`."

    normalized = normalize_raw_operation(raw)
    action_name = str(normalized.get("action") or "").strip()
    try:
        action = EditAction(action_name)
    except ValueError:
        allowed = ", ".join(EDIT_ACTION_CHOICES)
        return (
            f"{operation_label} uses unknown action `{action_name or '<empty>'}`. "
            f"Allowed actions: {allowed}."
        )

    missing_fields = [field for field in _ACTION_REQUIRED_FIELDS[action] if not _field_is_present(normalized, field)]
    if missing_fields:
        return (
            f"{operation_label} for action `{action.value}` is missing required fields: "
            + ", ".join(missing_fields)
            + "."
        )

    return None


def validate_edit_plan_payload(payload: dict[str, Any]) -> str | None:
    """Validate the semantic shape of an edit plan before the coding worker applies it."""

    payload = normalize_edit_plan_payload(payload)
    operations = payload.get("operations")
    if not isinstance(operations, list):
        return "The `operations` field must be a list for the edit_plan contract."

    if not operations:
        blocking_reason = str(payload.get("blocking_reason") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        if _looks_like_generic_empty_plan(summary=summary, blocking_reason=blocking_reason):
            return (
                "The edit plan returned zero operations together with a generic blocker that ignores the concrete "
                "coding goal. Return either a concrete file operation or a blocker tied to a specific candidate file."
            )

    errors: list[str] = []
    for index, raw in enumerate(operations):
        validation_error = validate_raw_operation(raw, index=index)
        if validation_error:
            errors.append(validation_error)
        if len(errors) >= 3:
            break

    if errors:
        return "Invalid edit-plan operations: " + " | ".join(errors)
    return None


def _field_is_present(payload: dict[str, Any], field_name: str) -> bool:
    """Check whether one required operation field is present with a usable value."""

    value = payload.get(field_name)
    if field_name in {"file_path", "reason", "symbol_name", "anchor_text"}:
        return isinstance(value, str) and bool(value.strip())
    if field_name in {"start_line", "end_line"}:
        return isinstance(value, int) and value >= 1
    if field_name == "new_content":
        return isinstance(value, str)
    return value is not None


def _looks_like_generic_empty_plan(*, summary: str, blocking_reason: str) -> bool:
    """Detect off-task empty edit plans that look like generic assistant replies instead of coding decisions."""

    combined = _normalize_text_for_pattern_match(f"{summary}\n{blocking_reason}")
    if not combined:
        return False
    return any(pattern in combined for pattern in _GENERIC_EMPTY_PLAN_PATTERNS)


def _normalize_text_for_pattern_match(text: str) -> str:
    """Lowercase and de-accent free text so German and English empty-plan markers match reliably."""

    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    collapsed = re.sub(r"\s+", " ", ascii_only).strip().lower()
    return collapsed


def _expand_one_raw_operation(raw: Any) -> list[dict[str, Any]]:
    """Expand one raw operation or one nested operation container into flat edit operations."""

    if not isinstance(raw, dict):
        return [raw]

    nested_changes = raw.get("changes")
    if not isinstance(nested_changes, list) or raw.get("action"):
        return [normalize_raw_operation(raw)]

    defaults: dict[str, Any] = {}
    for source_key, target_key in (
        ("file_path", "file_path"),
        ("file", "file_path"),
        ("path", "file_path"),
        ("reason", "reason"),
        ("description", "reason"),
        ("details", "reason"),
    ):
        value = raw.get(source_key)
        if isinstance(value, str) and value.strip() and target_key not in defaults:
            defaults[target_key] = value.strip()

    expanded: list[dict[str, Any]] = []
    for change in nested_changes:
        if not isinstance(change, dict):
            continue
        merged = dict(defaults)
        merged.update(change)
        expanded.append(normalize_raw_operation(merged))

    return expanded or [normalize_raw_operation(raw)]


def _apply_location_aliases(payload: dict[str, Any]) -> None:
    """Map one nested `location` object into flat edit-plan fields when present."""

    location = payload.pop("location", None)
    if not isinstance(location, dict):
        return

    for alias in ("file_path", "file", "path"):
        value = location.get(alias)
        if isinstance(value, str) and value.strip() and "file_path" not in payload:
            payload["file_path"] = value.strip()
            break

    location_type = str(location.get("type") or "").strip().lower()
    location_name = str(location.get("name") or location.get("symbol") or "").strip()
    if location_name and "symbol_name" not in payload and location_type in {"function", "method", "class", "symbol"}:
        payload["symbol_name"] = location_name
        parent_symbol = location.get("parent_symbol") or location.get("parent") or location.get("class_name")
        if isinstance(parent_symbol, str) and parent_symbol.strip():
            payload["parent_symbol"] = parent_symbol.strip()

    start_line = _as_positive_int(
        location.get("start_line") or location.get("line_start") or location.get("start")
    )
    end_line = _as_positive_int(
        location.get("end_line") or location.get("line_end") or location.get("end")
    )
    if start_line is not None and "start_line" not in payload:
        payload["start_line"] = start_line
    if end_line is not None and "end_line" not in payload:
        payload["end_line"] = end_line

    anchor_text = location.get("anchor_text") or location.get("anchor") or location.get("snippet")
    if isinstance(anchor_text, str) and anchor_text.strip() and "anchor_text" not in payload:
        payload["anchor_text"] = anchor_text


def _normalize_action_aliases(payload: dict[str, Any]) -> None:
    """Map loose action names from local models onto the canonical edit action set."""

    raw_action = str(payload.get("action") or "").strip().lower()
    if not raw_action:
        return

    action_aliases = {
        "update": EditAction.CREATE_OR_UPDATE.value,
        "modify": EditAction.CREATE_OR_UPDATE.value,
        "edit": EditAction.CREATE_OR_UPDATE.value,
        "rewrite": EditAction.CREATE_OR_UPDATE.value,
    }
    if raw_action in action_aliases:
        payload["action"] = action_aliases[raw_action]
        return

    if raw_action == "validate":
        payload.pop("action", None)


def _infer_operation_action(payload: dict[str, Any]) -> str:
    """Choose the most specific canonical action that matches the normalized fields."""

    existing_action = payload.get("action")
    if isinstance(existing_action, str) and existing_action.strip():
        return existing_action.strip()
    if isinstance(payload.get("symbol_name"), str) and isinstance(payload.get("new_content"), str):
        return EditAction.REPLACE_SYMBOL_BODY.value
    if (
        isinstance(payload.get("start_line"), int)
        and isinstance(payload.get("end_line"), int)
        and isinstance(payload.get("new_content"), str)
    ):
        return EditAction.REPLACE_LINES.value
    if isinstance(payload.get("anchor_text"), str) and isinstance(payload.get("new_content"), str):
        return EditAction.REPLACE_BLOCK.value
    return EditAction.CREATE_OR_UPDATE.value


def _as_positive_int(value: Any) -> int | None:
    """Convert one loose line number field into a usable positive integer."""

    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed >= 1 else None
    return None
