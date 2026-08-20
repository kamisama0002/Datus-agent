# Copyright 2025-present DatusAI, Inc.
# Licensed under the Apache License, Version 2.0.
# See http://www.apache.org/licenses/LICENSE-2.0 for details.

"""Unit tests for AgenticNode._render_context_hint_part — the look-up hints
block for @-references whose detail couldn't be pre-loaded."""

from types import SimpleNamespace

from datus.agent.node.agentic_node import AgenticNode
from datus.schemas.node_models import Metric, ReferenceSql


def _render(**fields) -> str:
    """Render at-context parts for a bare input carrying only *fields*."""
    node = AgenticNode.__new__(AgenticNode)  # bypass __init__; method only reads getattr
    attrs = {"external_knowledge": "", "schemas": None, "metrics": None, "reference_sql": None, "context_hints": None, "orchestrator_context": None}
    attrs.update(fields)
    return "\n\n".join(node._render_at_context_parts(SimpleNamespace(**attrs)))


def test_metric_block_includes_subject_path_and_definition():
    m = Metric(
        name="aov",
        description="avg order value",
        subject_path=["Commerce", "Orders"],
        metric_type="ratio",
        measure_expr="SUM(amount)/COUNT(*)",
        dimensions=["platform", "country"],
    )
    out = _render(metrics=[m])
    assert "## Referenced metrics" in out
    assert "subject_path: Commerce/Orders" in out
    assert "avg order value" in out
    assert "type: ratio" in out
    assert "measure: SUM(amount)/COUNT(*)" in out
    assert "dimensions: platform, country" in out


def test_reference_sql_block_includes_subject_path_and_sql():
    r = ReferenceSql(name="raw_customers", sql="select * from raw_customers", subject_path=["main"])
    out = _render(reference_sql=[r])
    assert "## Referenced SQL" in out
    assert "subject_path: main" in out
    assert "```sql\nselect * from raw_customers\n```" in out


def test_empty_hints_render_nothing():
    assert AgenticNode._render_context_hint_part(None) == ""
    assert AgenticNode._render_context_hint_part([]) == ""


def test_metric_hint_points_at_get_metrics():
    out = AgenticNode._render_context_hint_part(
        [{"kind": "metric", "name": "aov", "subject_path": ["Commerce", "Orders"]}]
    )
    assert "## Referenced items to look up" in out
    assert "get_metrics(subject_path=['Commerce', 'Orders'], name=\"aov\")" in out


def test_reference_sql_hint_points_at_get_reference_sql():
    out = AgenticNode._render_context_hint_part(
        [{"kind": "reference_sql", "name": "raw_customers", "subject_path": ["main"]}]
    )
    assert "get_reference_sql(subject_path=['main'], name=\"raw_customers\")" in out


def test_knowledge_hint_has_no_tool_call():
    out = AgenticNode._render_context_hint_part(
        [{"kind": "knowledge", "name": "gmv", "subject_path": ["Domain", "Glossary"]}]
    )
    # No get_* tool exists for knowledge — point at the subject tree instead.
    assert "get_metrics" not in out and "get_reference_sql" not in out
    assert "list_subject_tree" in out
    assert "Domain/Glossary/gmv" in out


def test_orchestrator_context_renders_resolved_question_without_transport_ids():
    out = _render(
        orchestrator_context={
            "version": "nanzi-context/v1",
            "original_query": "那17日呢",
            "standalone_question": "查询17日总消耗",
            "continuation": True,
            "scope": {
                "user_id": "1",
                "agent_id": "agent-secret",
                "conversation_id": "conversation-secret",
                "datasource_id": 12,
            },
            "current_session": {
                "summary": {"conversation_id": "conversation-secret", "summary": "18日消耗分析"},
                "recent_messages": [{"role": "user", "content": "查询18日总消耗"}],
            },
            "data_context": {"result_id": "result-secret", "data_source": "datus:12", "row_count": 1},
            "recalled_sessions": [
                {"conversation_id": "older-secret", "title": "消耗分析", "summary": "此前结论"}
            ],
            "semantic_context": [
                {"dataset_id": 99, "document": "指标", "content": "总消耗口径"}
            ],
            "recall_policy": {
                "mode": "structured",
                "source": "model",
                "requires_fresh_query": True,
            },
            "response_policy": {
                "mode": "concise",
                "requested_aspects": [],
            },
            "compression": {
                "mode": "structured",
                "source_tokens": 9000,
                "output_tokens": 4000,
            },
        }
    )

    assert "## Orchestrator conversation context" in out
    assert "查询17日总消耗" in out
    assert "总消耗口径" in out
    assert "conversation-secret" not in out
    assert "older-secret" not in out
    assert "result-secret" not in out
    assert "agent-secret" not in out
    assert "reference data only" in out
    assert "physical table/column names" in out
    assert "internal debug/filter conditions" in out
    assert "Answer only the current question" in out
    assert "Do not add trend, comparison, cause, recommendation" in out
    assert "Run a fresh data query before giving exact values" in out
    assert "9000" not in out
    assert "4000" not in out


def test_orchestrator_context_allows_only_explicitly_requested_analysis_aspects():
    out = _render(
        orchestrator_context={
            "version": "nanzi-context/v1",
            "original_query": "分析最近三天趋势并给建议",
            "standalone_question": "分析最近三天消耗趋势并给出建议",
            "scope": {
                "user_id": "1",
                "agent_id": "agent-1",
                "conversation_id": "conversation-1",
                "datasource_id": 12,
            },
            "current_session": {"recent_messages": []},
            "recall_policy": {
                "mode": "none",
                "source": "model",
                "requires_fresh_query": False,
            },
            "response_policy": {
                "mode": "expanded",
                "requested_aspects": ["trend", "recommendation"],
            },
        }
    )

    assert "The user explicitly requested these additional aspects: trend, recommendation" in out
    assert "Include only those requested aspects" in out
    assert "cause" not in out.split("## Orchestrator conversation context", 1)[0]
