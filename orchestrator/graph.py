"""Pure helpers over a workflow graph (a list of ``StageSpec``).

Nothing here touches runtime state, so it is trivial to unit-test.
"""
from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping

from .models import StageSpec, StageState

VALID_JOINS = ("all", "any")

# Outcomes of dependency_status().
READY, WAIT, DEAD = "ready", "wait", "dead"

# A dependency in one of these states can never produce output (until a re-plan).
DEAD_STATES = {
    StageState.FAILED,
    StageState.ROLLED_BACK,
    StageState.BLOCKED,
    StageState.SKIPPED,
}


def validate_graph(specs: Iterable[StageSpec]) -> None:
    """Raise ``ValueError`` on duplicate ids, unknown/self dependencies,
    bad ``join`` / ``max_attempts`` values, or cycles."""
    specs = list(specs)
    if not specs:
        raise ValueError("graph has no stages")
    ids = [s.id for s in specs]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate stage ids: {duplicates}")
    known = set(ids)
    for s in specs:
        unknown = [d for d in s.depends_on if d not in known]
        if unknown:
            raise ValueError(f"stage '{s.id}' depends on unknown stage(s): {unknown}")
        if s.id in s.depends_on:
            raise ValueError(f"stage '{s.id}' depends on itself")
        if s.join not in VALID_JOINS:
            raise ValueError(f"stage '{s.id}' has invalid join '{s.join}' (use {VALID_JOINS})")
        if s.max_attempts < 1:
            raise ValueError(f"stage '{s.id}' needs max_attempts >= 1")
    topological_order(specs)  # raises on a cycle


def topological_order(specs: Iterable[StageSpec]) -> list[str]:
    """Kahn's algorithm. Stable: ties keep the order the specs were given in.
    Raises ``ValueError`` if the graph has a cycle."""
    specs = list(specs)
    indegree = {s.id: len(set(s.depends_on)) for s in specs}
    children: dict[str, list[str]] = {s.id: [] for s in specs}
    for s in specs:
        for d in set(s.depends_on):
            children[d].append(s.id)
    queue = deque(i for i in (s.id for s in specs) if indegree[i] == 0)
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(specs):
        stuck = sorted(set(indegree) - set(order))
        raise ValueError(f"cycle detected among stages: {stuck}")
    return order


def downstream(specs: Iterable[StageSpec], stage_id: str) -> set[str]:
    """All stages that transitively depend on ``stage_id`` (excluding itself)."""
    children: dict[str, list[str]] = {}
    for s in specs:
        for d in s.depends_on:
            children.setdefault(d, []).append(s.id)
    seen: set[str] = set()
    stack = list(children.get(stage_id, []))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(children.get(node, []))
    return seen


def dependency_status(spec: StageSpec, states: Mapping[str, StageState]) -> str:
    """Can ``spec`` start? Returns READY, WAIT (some dependency unfinished) or
    DEAD (it can never start because upstream failed / was skipped).

    join="all": every dependency must succeed; one dead dependency kills it.
    join="any": one succeeded dependency is enough; dead only if all are dead.
    """
    if not spec.depends_on:
        return READY
    dep_states = [states[d] for d in spec.depends_on]
    succeeded = [s == StageState.SUCCEEDED for s in dep_states]
    dead = [s in DEAD_STATES for s in dep_states]
    if spec.join == "any":
        if any(succeeded):
            return READY
        return DEAD if all(dead) else WAIT
    if any(dead):
        return DEAD
    return READY if all(succeeded) else WAIT
