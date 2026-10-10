import pytest

from orchestrator.graph import (
    DEAD, READY, WAIT, dependency_status, downstream, topological_order, validate_graph,
)
from orchestrator.models import StageSpec, StageState as S


def spec(id, deps=(), **kw):
    return StageSpec(id=id, title=id, agent=id, depends_on=list(deps), **kw)


DIAMOND = [spec("a"), spec("b", ["a"]), spec("c", ["a"]), spec("d", ["b", "c"])]


def test_valid_graph_passes():
    validate_graph(DIAMOND)


@pytest.mark.parametrize("specs, message", [
    ([], "no stages"),
    ([spec("a"), spec("a")], "duplicate"),
    ([spec("a", ["ghost"])], "unknown"),
    ([spec("a", ["a"])], "itself"),
    ([spec("a", ["b"]), spec("b", ["a"])], "cycle"),
    ([spec("a"), spec("b", ["c"]), spec("c", ["d"]), spec("d", ["b"])], "cycle"),
    ([spec("a", join="some")], "join"),
    ([spec("a", max_attempts=0)], "max_attempts"),
])
def test_invalid_graphs_rejected(specs, message):
    with pytest.raises(ValueError, match=message):
        validate_graph(specs)


def test_topological_order_respects_dependencies():
    order = topological_order(list(reversed(DIAMOND)))
    assert order.index("a") < order.index("b") < order.index("d")
    assert order.index("a") < order.index("c") < order.index("d")


def test_topological_order_is_stable_for_independent_stages():
    assert topological_order([spec("x"), spec("y"), spec("z")]) == ["x", "y", "z"]


def test_downstream_is_transitive_and_excludes_self():
    assert downstream(DIAMOND, "a") == {"b", "c", "d"}
    assert downstream(DIAMOND, "b") == {"d"}
    assert downstream(DIAMOND, "d") == set()


def test_dependency_status_join_all():
    s = spec("d", ["b", "c"])
    assert dependency_status(spec("a"), {}) == READY
    assert dependency_status(s, {"b": S.SUCCEEDED, "c": S.SUCCEEDED}) == READY
    assert dependency_status(s, {"b": S.SUCCEEDED, "c": S.RUNNING}) == WAIT
    assert dependency_status(s, {"b": S.SUCCEEDED, "c": S.WAITING_APPROVAL}) == WAIT
    assert dependency_status(s, {"b": S.FAILED, "c": S.RUNNING}) == DEAD   # no point waiting


def test_dependency_status_join_any():
    s = spec("d", ["b", "c"], join="any")
    assert dependency_status(s, {"b": S.SUCCEEDED, "c": S.RUNNING}) == READY
    assert dependency_status(s, {"b": S.FAILED, "c": S.RUNNING}) == WAIT
    assert dependency_status(s, {"b": S.FAILED, "c": S.SKIPPED}) == DEAD


@pytest.mark.parametrize("dead", [S.FAILED, S.ROLLED_BACK, S.BLOCKED, S.SKIPPED])
def test_all_dead_states_kill_dependents(dead):
    assert dependency_status(spec("b", ["a"]), {"a": dead}) == DEAD
