"""Unit tests for E1 subtask DAG parse + validate (dag_materialize service)."""

from __future__ import annotations

import json

import pytest

from app.services.dag_materialize import (
    MAX_CHILDREN,
    MAX_DEPTH,
    DagValidationError,
    Subtask,
    parse_subtasks_payload,
    validate_subtasks,
    _has_cycle,
    _max_depth,
)


class TestParseSubtasksPayload:
    def test_happy_path(self) -> None:
        raw = json.dumps([
            {"title": "A", "deps": []},
            {"title": "B", "deps": [0]},
            {"title": "C", "deps": [0, 1]},
        ])
        items = parse_subtasks_payload(raw)
        assert len(items) == 3
        assert items[0].title == "A"
        assert items[1].deps == [0]
        assert items[2].deps == [0, 1]

    def test_optional_fields(self) -> None:
        raw = json.dumps([
            {
                "title": "A",
                "deps": [],
                "description": "first step",
                "priority": "high",
                "complexity": 2,
            },
        ])
        items = parse_subtasks_payload(raw)
        assert items[0].description == "first step"
        assert items[0].priority == "high"
        assert items[0].complexity == 2

    def test_empty_payload_rejected(self) -> None:
        with pytest.raises(DagValidationError, match="empty"):
            parse_subtasks_payload("")
        with pytest.raises(DagValidationError, match="empty"):
            parse_subtasks_payload("   ")

    def test_malformed_json(self) -> None:
        with pytest.raises(DagValidationError, match="invalid JSON"):
            parse_subtasks_payload("not json")

    def test_non_array_root(self) -> None:
        with pytest.raises(DagValidationError, match="must be a JSON array"):
            parse_subtasks_payload('{"title": "A"}')

    def test_empty_array(self) -> None:
        with pytest.raises(DagValidationError, match="empty"):
            parse_subtasks_payload("[]")

    def test_missing_title(self) -> None:
        raw = json.dumps([{"deps": []}])
        with pytest.raises(DagValidationError, match="title"):
            parse_subtasks_payload(raw)

    def test_blank_title(self) -> None:
        raw = json.dumps([{"title": "  ", "deps": []}])
        with pytest.raises(DagValidationError, match="title"):
            parse_subtasks_payload(raw)

    def test_deps_must_be_list_of_ints(self) -> None:
        raw = json.dumps([{"title": "A", "deps": ["x"]}])
        with pytest.raises(DagValidationError, match="deps"):
            parse_subtasks_payload(raw)

    def test_priority_wrong_type(self) -> None:
        raw = json.dumps([{"title": "A", "deps": [], "priority": 99}])
        with pytest.raises(DagValidationError, match="priority"):
            parse_subtasks_payload(raw)

    def test_complexity_wrong_type(self) -> None:
        raw = json.dumps([{"title": "A", "deps": [], "complexity": "high"}])
        with pytest.raises(DagValidationError, match="complexity"):
            parse_subtasks_payload(raw)


class TestValidateSubtasks:
    def test_happy_dag(self) -> None:
        items = [
            Subtask(title="A", deps=[]),
            Subtask(title="B", deps=[0]),
            Subtask(title="C", deps=[0, 1]),
        ]
        validate_subtasks(items)  # no raise

    def test_too_many_children(self) -> None:
        items = [Subtask(title=f"x{i}", deps=[]) for i in range(MAX_CHILDREN + 1)]
        with pytest.raises(DagValidationError, match="child limit"):
            validate_subtasks(items)

    def test_dep_index_out_of_range(self) -> None:
        items = [Subtask(title="A", deps=[5])]
        with pytest.raises(DagValidationError, match="out of range"):
            validate_subtasks(items)

    def test_self_dep_rejected(self) -> None:
        items = [Subtask(title="A", deps=[0])]
        with pytest.raises(DagValidationError, match="cannot depend on itself"):
            validate_subtasks(items)

    def test_simple_cycle(self) -> None:
        # 0→1 and 1→0
        items = [
            Subtask(title="A", deps=[1]),
            Subtask(title="B", deps=[0]),
        ]
        with pytest.raises(DagValidationError, match="cycle"):
            validate_subtasks(items)

    def test_indirect_cycle(self) -> None:
        # 0→1→2→0
        items = [
            Subtask(title="A", deps=[2]),
            Subtask(title="B", deps=[0]),
            Subtask(title="C", deps=[1]),
        ]
        with pytest.raises(DagValidationError, match="cycle"):
            validate_subtasks(items)

    def test_depth_exceeded(self) -> None:
        # Chain a→b→c→d → depth 4, MAX_DEPTH=3
        items = [
            Subtask(title="a", deps=[]),
            Subtask(title="b", deps=[0]),
            Subtask(title="c", deps=[1]),
            Subtask(title="d", deps=[2]),
        ]
        with pytest.raises(DagValidationError, match="depth"):
            validate_subtasks(items)

    def test_max_allowed_depth(self) -> None:
        # depth exactly = MAX_DEPTH=3 ok
        items = [
            Subtask(title="a", deps=[]),
            Subtask(title="b", deps=[0]),
            Subtask(title="c", deps=[1]),
        ]
        validate_subtasks(items)  # no raise


class TestGraphHelpers:
    def test_has_cycle_self_loop(self) -> None:
        # self-loop caught by validate (not _has_cycle which has visited guard).
        # _has_cycle on a 0→0 would re-enter and return True via in_stack.
        items = [Subtask(title="A", deps=[0])]
        assert _has_cycle(items) is True

    def test_has_cycle_dag(self) -> None:
        items = [
            Subtask(title="A", deps=[]),
            Subtask(title="B", deps=[0]),
            Subtask(title="C", deps=[0, 1]),
        ]
        assert _has_cycle(items) is False

    def test_max_depth_linear(self) -> None:
        items = [
            Subtask(title="a", deps=[]),
            Subtask(title="b", deps=[0]),
            Subtask(title="c", deps=[1]),
        ]
        assert _max_depth(items) == 3

    def test_max_depth_parallel(self) -> None:
        # Both B and C only depend on A; depth = 2
        items = [
            Subtask(title="A", deps=[]),
            Subtask(title="B", deps=[0]),
            Subtask(title="C", deps=[0]),
        ]
        assert _max_depth(items) == 2

    def test_max_depth_diamond(self) -> None:
        # A → B, A → C, then D depends on B+C → depth 3
        items = [
            Subtask(title="A", deps=[]),
            Subtask(title="B", deps=[0]),
            Subtask(title="C", deps=[0]),
            Subtask(title="D", deps=[1, 2]),
        ]
        assert _max_depth(items) == 3
