"""Native Datus SQL-policy provider for immutable NanZi projects."""

from __future__ import annotations

import re
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from datus.tools.sql_policy import EnforcementResult, SqlPolicyConfig


_ALLOWED_STATEMENT_TYPES = ["select", "cte", "explain"]
_MAX_ROWS = 1000
_MAX_RESULT_BYTES = 2 * 1024 * 1024
_TIMEOUT_SECONDS = 60


def _first_keyword(sql: str) -> str:
    """Return the first token after leading whitespace and SQL comments."""
    remaining = sql
    while True:
        remaining = remaining.lstrip()
        if remaining.startswith(("--", "#")):
            _, separator, remaining = remaining.partition("\n")
            if not separator:
                return ""
            continue
        if remaining.startswith("/*"):
            end = remaining.find("*/", 2)
            if end < 0:
                return ""
            remaining = remaining[end + 2 :]
            continue
        match = re.match(r"[A-Za-z]+", remaining)
        return match.group(0).upper() if match else ""


class NanziReadOnlySqlPolicy:
    """Fail-closed policy loaded by Datus's existing SQL-policy seam."""

    def __init__(self, config: SqlPolicyConfig) -> None:
        expected: dict[str, Any] = {
            "enabled": True,
            "provider": "nanzi_datus_bridge.query_policy:NanziReadOnlySqlPolicy",
            "allowed_statement_types": _ALLOWED_STATEMENT_TYPES,
            "timeout_seconds": _TIMEOUT_SECONDS,
            "max_rows": _MAX_ROWS,
            "max_result_bytes": _MAX_RESULT_BYTES,
        }
        if config.raw != expected:
            raise TypeError("NanZi SQL policy configuration is incompatible")

    def enforce_read(
        self,
        sql: str,
        *,
        datasource: str,
        dialect: str,
        principal: dict[str, Any] | None,
    ) -> EnforcementResult:
        del datasource, principal
        keyword = _first_keyword(sql)
        if keyword == "EXPLAIN":
            return EnforcementResult(allowed=True, sql=sql, applied_policies=["nanzi-read-only"])
        if keyword not in {"SELECT", "WITH"}:
            return EnforcementResult(allowed=False, reason="statement type is not allowed")
        try:
            expression = parse_one(sql, read=dialect or None)
        except (ParseError, ValueError):
            return EnforcementResult(allowed=False, reason="statement could not be classified")
        if not isinstance(expression, exp.Query):
            return EnforcementResult(allowed=False, reason="statement type is not allowed")

        existing_limit = expression.args.get("limit")
        if existing_limit is not None:
            value = existing_limit.expression
            if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= _MAX_ROWS:
                return EnforcementResult(allowed=True, sql=sql, applied_policies=["nanzi-read-only"])
        bounded = expression.limit(_MAX_ROWS, copy=True).sql(dialect=dialect or None)
        return EnforcementResult(
            allowed=True,
            sql=bounded,
            applied_policies=["nanzi-read-only", "nanzi-max-rows"],
        )
