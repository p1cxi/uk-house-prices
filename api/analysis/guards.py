"""Validation for the run_sql escape hatch.

The real safety guarantee is the agent_ro role (default_transaction_read_only=on,
statement_timeout, SELECT-only grants). These checks are a cheap second layer that
gives the LLM a clear error before the query ever reaches Postgres.
"""
import re

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|call|do|"
    r"merge|vacuum|analyze|reindex|refresh|comment|set|reset)\b",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def validate_readonly_sql(sql: str):
    """Return (True, cleaned_sql) or (False, error_message)."""
    if not sql or not sql.strip():
        return False, "empty SQL"
    cleaned = _COMMENT.sub(" ", sql).strip().rstrip(";").strip()
    if ";" in cleaned:
        return False, "only a single statement is allowed"
    head = cleaned.lstrip("(").lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        return False, "only SELECT / WITH queries are allowed"
    if _FORBIDDEN.search(cleaned):
        return False, "query contains a forbidden (non-read-only) keyword"
    return True, cleaned


def wrap_with_limit(sql: str, max_rows: int) -> str:
    return f"SELECT * FROM (\n{sql}\n) _agent LIMIT {int(max_rows)}"
