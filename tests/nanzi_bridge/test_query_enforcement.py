from __future__ import annotations

import pytest

from datus.schemas.node_models import ExecuteSQLResult
from datus.tools.db_tools.db_manager import DBManager
from datus.tools.func_tool.database import DBFuncTool
from nanzi_datus_bridge.config_builder import NanziConfigBuilder
from tests.nanzi_bridge.conftest import project_config


class _Connector:
    dialect = "mysql"

    def __init__(self, result: ExecuteSQLResult) -> None:
        self.result = result
        self.queries: list[str] = []

    def execute_query(self, sql: str, *, result_format: str) -> ExecuteSQLResult:
        self.queries.append(sql)
        return self.result


def _tool():
    tool = object.__new__(DBFuncTool)
    tool.agent_config = NanziConfigBuilder().build_agent_config(project_config())
    tool._scoped_patterns = []
    tool._default_datasource = "nanzi_17"
    return tool


def test_disallowed_sql_never_reaches_native_connector() -> None:
    connector = _Connector(ExecuteSQLResult(success=True, sql_return=[]))

    results = [
        _tool().execute_read_enforced(sql, connector)
        for sql in ("DELETE FROM orders", "SHOW TABLES")
    ]

    assert all(result.success is False for result in results)
    assert connector.queries == []


def test_native_policy_rewrites_row_limit_and_bounds_connector_result() -> None:
    rows = [{"id": number} for number in range(1200)]
    connector = _Connector(ExecuteSQLResult(success=True, row_count=len(rows), sql_return=rows, result_format="list"))

    result = _tool().execute_read_enforced("SELECT id FROM orders", connector)

    assert connector.queries
    assert "LIMIT 1000" in connector.queries[0].upper()
    assert result.success is True
    assert result.row_count == 1000
    assert len(result.sql_return) == 1000


def test_native_result_hook_fails_closed_before_returning_oversized_bytes() -> None:
    oversized = [{"payload": "x" * (2 * 1024 * 1024)}]
    connector = _Connector(ExecuteSQLResult(success=True, row_count=1, sql_return=oversized, result_format="list"))

    result = _tool().execute_read_enforced("SELECT payload FROM orders", connector)

    assert result.success is False
    assert result.sql_return is None
    assert result.row_count == 0
    assert "limit" in (result.error or "").lower()


def test_native_result_hook_fails_closed_for_oversized_optional_pandas_dataframe() -> None:
    pandas = pytest.importorskip("pandas")
    dataframe = pandas.DataFrame({"payload": ["x" * (2 * 1024 * 1024)]})
    connector = _Connector(
        ExecuteSQLResult(success=True, row_count=1, sql_return=dataframe, result_format="dataframe")
    )

    result = _tool().execute_read_enforced("SELECT payload FROM orders", connector)

    assert result.success is False
    assert result.sql_return is None
    assert result.row_count == 0
    assert "limit" in (result.error or "").lower()


def test_timeout_is_propagated_into_native_connection_config() -> None:
    agent_config = NanziConfigBuilder().build_agent_config(project_config())
    manager = object.__new__(DBManager)

    connection_config = manager._db_config_to_connection_config(agent_config.current_db_config())

    assert connection_config["timeout_seconds"] == 60
