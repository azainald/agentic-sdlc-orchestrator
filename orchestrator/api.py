"""HTTP API of the orchestrator.

Routes only translate HTTP <-> engine calls; everything they need lives in one
``Ctx`` object on ``app.state``, so every ``create_orchestrator_app()`` call is
a fully isolated app (this is what the tests rely on).

Background execution, in short: ``POST /runs`` creates the run (cheap, sync),
then starts ``engine.run(id)`` as an asyncio task and answers ``202`` straight
away. ``approve``/``reject`` do the same with ``engine.approve/reject``. The
``Supervisor`` below owns those tasks (strong references, cleanup on finish, a
failure handler, cancellation on shutdown). Clients either poll ``GET /runs/{id}``
or add ``?wait=true`` to block until the run is terminal or waiting for a human.
All engine calls happen on the event loop (the engine is not thread-safe), so
every route is ``async def``.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional, Union

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, StringConstraints
from starlette.exceptions import HTTPException as StarletteHTTPException
from typing_extensions import Annotated

from scenarios.base import Scenario

from .contracts import RunStore, TelemetrySink
from .models import (
    TERMINAL_RUN_STATES,
    TERMINAL_STAGE_STATES,
    Approval,
    ApprovalStatus,
    DecisionRecord,
    Run,
    RunMetrics,
    RunState,
    StageSpec,
    StageState,
    now,
)
from .policy import PolicyViolation
from .telemetry import to_prometheus_text

log = logging.getLogger("orchestrator.api")

ScenarioSource = Union[dict[str, Scenario], Callable[[], dict[str, Scenario]]]
WAIT_STATES = TERMINAL_RUN_STATES | {RunState.WAITING_APPROVAL}


# ============================================================================ errors
class ApiError(Exception):
    """Carries what is needed to build the ``{"error": {"code", "message"}}`` body."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def install_error_handlers(app: FastAPI) -> None:
    """Every error (ours, FastAPI's, Starlette's) gets the same JSON shape."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"] if p != "body")
        return error_response(422, "validation_error", f"{where}: {first['msg']}" if where else first["msg"])

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


# ============================================================================ schemas
class CreateRunRequest(BaseModel):
    scenario: str = Field(description="Name of a scenario from GET /scenarios")
    requirement: Optional[str] = Field(default=None, min_length=1, description="Overrides the scenario's requirement")
    params: dict[str, Any] = Field(default_factory=dict, description="Merged over the scenario's default params")


class ApprovalDecision(BaseModel):
    """Approvals are always attributed to a named human (or system) actor."""

    actor: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    comment: str = Field(default="", max_length=2000)


class HealthResponse(BaseModel):
    status: str = "ok"


class ScenarioView(BaseModel):
    name: str
    title: str
    description: str
    requirement: str
    params: dict[str, Any]
    stages: list[StageSpec]          # the explicit DAG: depends_on, join, gates, approvals ...


class RunAccepted(BaseModel):
    run_id: str
    state: RunState
    links: dict[str, str]


class ApprovalAccepted(BaseModel):
    run_id: str
    approval_id: str
    decision: ApprovalStatus
    actor: str
    links: dict[str, str]


class RunSummary(BaseModel):
    run_id: str
    scenario: str
    state: RunState
    created_at: float
    links: dict[str, str]


class StageView(BaseModel):
    id: str
    title: str
    agent: str
    depends_on: list[str]
    state: StageState
    attempts: int
    runs: int
    used_fallback: bool
    error: Optional[str]
    requires_approval: bool
    approval_kind: Optional[str]
    started_at: Optional[float]
    ended_at: Optional[float]
    duration_ms: Optional[float]


class ArtifactView(BaseModel):
    name: str
    version: int
    sha256: str
    produced_by: str
    produced_at: float
    content: Optional[dict[str, Any]] = Field(default=None, description="Only with ?include_content=true")


class RunView(BaseModel):
    run_id: str
    scenario: str
    requirement: str
    state: RunState
    stop_requested: bool
    created_at: float
    params: dict[str, Any]
    stages: list[StageView]
    artifacts: list[ArtifactView]
    pending_approvals: list[Approval]
    approvals: list[Approval]
    metrics: RunMetrics
    links: dict[str, str]


class TraceView(BaseModel):
    run_id: str
    events: list[dict[str, Any]] = Field(description="Telemetry audit events, in order")
    decisions: list[DecisionRecord] = Field(description="Decision lineage kept on the run")


class DagNode(BaseModel):
    id: str
    title: str
    agent: str
    state: StageState
    requires_approval: bool
    approval_kind: Optional[str]
    join: str


class DagEdge(BaseModel):
    source: str
    target: str


class DagView(BaseModel):
    run_id: str
    state: RunState
    nodes: list[DagNode]
    edges: list[DagEdge]


# ============================================================================ supervisor
class Supervisor:
    """Owns the background tasks that execute runs.

    * strong references (asyncio only keeps weak ones) removed when a task ends;
    * an exception in a task is logged AND recorded on the run as a failed
      ``finish`` event instead of vanishing with the task;
    * ``wait`` lets a request block until a run settles;
    * ``shutdown`` cancels whatever is still running.
    """

    def __init__(self, engine: Any, telemetry: TelemetrySink):
        self.engine, self.telemetry = engine, telemetry
        self._tasks: dict[str, set[asyncio.Task]] = {}
        self.resolving: set[tuple[str, str]] = set()   # approvals being applied right now

    def launch(self, run_id: str, work: Awaitable[Any], resolving: Optional[tuple[str, str]] = None) -> None:
        if resolving:
            self.resolving.add(resolving)
        task = asyncio.create_task(self._guard(run_id, work, resolving))
        self._tasks.setdefault(run_id, set()).add(task)
        task.add_done_callback(lambda t: self._forget(run_id, t))

    def _forget(self, run_id: str, task: asyncio.Task) -> None:
        tasks = self._tasks.get(run_id, set())
        tasks.discard(task)
        if not tasks:
            self._tasks.pop(run_id, None)

    async def _guard(self, run_id: str, work: Awaitable[Any], resolving: Optional[tuple[str, str]]) -> None:
        try:
            await work
        except Exception as exc:  # CancelledError is not an Exception: shutdown still propagates
            log.exception("background execution of %s crashed", run_id)
            try:
                self._record_crash(run_id, exc)
            except Exception:
                log.exception("could not record the crash of %s", run_id)
        finally:
            self.resolving.discard(resolving)

    def _record_crash(self, run_id: str, exc: Exception) -> None:
        reason = f"background execution crashed: {exc!r}"
        run = self.engine.get_run(run_id)
        if run is None or run.state in TERMINAL_RUN_STATES:
            self.telemetry.emit(run_id, "finish", None, outcome="failed", rationale=reason)
            return
        for st in run.stages.values():
            if st.state not in TERMINAL_STAGE_STATES:
                st.state = StageState.SKIPPED
        for approval in run.approvals.values():
            if approval.status == ApprovalStatus.PENDING:
                approval.status, approval.actor = ApprovalStatus.REJECTED, "engine"
                approval.resolved_at, approval.comment = now(), reason
        run.state = RunState.FAILED
        if run.metrics.ended_at is None:
            run.metrics.ended_at = now()
            run.metrics.e2e_latency_ms = (run.metrics.ended_at - (run.metrics.started_at or run.metrics.ended_at)) * 1000
        run.decisions.append(DecisionRecord(run_id=run_id, kind="finish", rationale=reason, outcome="failed"))
        self.telemetry.emit(run_id, "finish", None, outcome="failed", rationale=reason)
        self.engine.store.save_run(run)
        self.telemetry.run_finished(run)

    async def wait(self, run_id: str, timeout: float) -> None:
        """Block until this run's tasks are done (= terminal or waiting for approval) or timeout."""
        tasks = list(self._tasks.get(run_id, ()))
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    @property
    def active(self) -> int:
        return sum(len(t) for t in self._tasks.values())

    async def shutdown(self) -> None:
        tasks = [t for group in self._tasks.values() for t in group]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class Ctx:
    engine: Any
    telemetry: TelemetrySink
    store: RunStore
    scenarios: ScenarioSource
    supervisor: Supervisor

    def scenario_map(self) -> dict[str, Scenario]:
        return self.scenarios() if callable(self.scenarios) else self.scenarios


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


# ============================================================================ view builders
def links_for(run_id: str) -> dict[str, str]:
    base = f"/runs/{run_id}"
    return {"self": base, "trace": f"{base}/trace", "dag": f"{base}/dag", "stop": f"{base}/stop"}


def run_view(run: Run, include_content: bool = False) -> RunView:
    stages = []
    for st in run.stages.values():
        duration = (st.ended_at - st.started_at) * 1000 if st.started_at and st.ended_at else None
        stages.append(StageView(
            id=st.spec.id, title=st.spec.title, agent=st.spec.agent, depends_on=st.spec.depends_on,
            state=st.state, attempts=st.attempts, runs=st.runs, used_fallback=st.used_fallback, error=st.error,
            requires_approval=st.spec.requires_approval, approval_kind=st.spec.approval_kind,
            started_at=st.started_at, ended_at=st.ended_at, duration_ms=duration,
        ))
    artifacts = [
        ArtifactView(name=a.name, version=a.version, sha256=a.sha256, produced_by=a.produced_by,
                     produced_at=a.produced_at, content=a.content if include_content else None)
        for a in run.artifacts.values()
    ]
    approvals = list(run.approvals.values())
    return RunView(
        run_id=run.id, scenario=run.scenario, requirement=run.requirement, state=run.state,
        stop_requested=run.stop_requested, created_at=run.created_at, params=run.params,
        stages=stages, artifacts=artifacts, approvals=approvals,
        pending_approvals=[a for a in approvals if a.status == ApprovalStatus.PENDING],
        metrics=run.metrics, links=links_for(run.id),
    )


def reply(model: BaseModel, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(model))


def find_run(ctx: Ctx, run_id: str) -> Run:
    run = ctx.engine.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"unknown run '{run_id}'")
    return run


ERRORS = {
    404: {"model": ErrorResponse, "description": "Unknown run, approval or scenario"},
    409: {"model": ErrorResponse, "description": "State conflict"},
    422: {"model": ErrorResponse, "description": "Invalid request"},
}
WaitParam = Query(False, description="Block until the run is terminal or waiting for approval")
TimeoutParam = Query(30.0, ge=0, le=600, description="Max seconds to block when wait=true")

router = APIRouter()


# ============================================================================ routes
@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/scenarios", response_model=list[ScenarioView], tags=["scenarios"])
async def list_scenarios(ctx: Ctx = Depends(get_ctx)) -> list[ScenarioView]:
    """Every runnable scenario with its explicit stage graph."""
    return [
        ScenarioView(name=name, title=s.title, description=s.description, requirement=s.requirement,
                     params=s.params, stages=s.graph)
        for name, s in ctx.scenario_map().items()
    ]


@router.post(
    "/runs", tags=["runs"], response_model=None, status_code=202,
    responses={202: {"model": RunAccepted, "description": "Run created and started in the background"},
               200: {"model": RunView, "description": "With wait=true: the run once settled (or on timeout)"},
               422: ERRORS[422]},
)
async def create_run(body: CreateRunRequest, wait: bool = WaitParam, timeout: float = TimeoutParam,
                     ctx: Ctx = Depends(get_ctx)) -> JSONResponse:
    scenarios = ctx.scenario_map()
    scenario = scenarios.get(body.scenario)
    if scenario is None:
        raise ApiError(422, "unknown_scenario", f"unknown scenario '{body.scenario}'; known: {sorted(scenarios)}")
    try:
        run = ctx.engine.create_run(scenario.name, body.requirement or scenario.requirement, scenario.graph,
                                    {**scenario.params, **body.params})
    except PolicyViolation as violation:
        raise ApiError(422, "policy_violation", violation.message)
    except ValueError as exc:
        raise ApiError(422, "invalid_graph", str(exc))
    ctx.supervisor.launch(run.id, ctx.engine.run(run.id))
    if wait:
        await ctx.supervisor.wait(run.id, timeout)
        return reply(run_view(find_run(ctx, run.id)), 200)
    return reply(RunAccepted(run_id=run.id, state=run.state, links=links_for(run.id)), 202)


@router.get("/runs", response_model=list[RunSummary], tags=["runs"])
async def list_runs(state: Optional[RunState] = Query(None, description="Filter by run state"),
                    ctx: Ctx = Depends(get_ctx)) -> list[RunSummary]:
    """Newest first."""
    runs = sorted(ctx.store.list_runs(), key=lambda r: r.created_at, reverse=True)
    return [RunSummary(run_id=r.id, scenario=r.scenario, state=r.state, created_at=r.created_at,
                       links=links_for(r.id)) for r in runs if state is None or r.state == state]


@router.get("/runs/{run_id}", response_model=RunView, tags=["runs"], responses={404: ERRORS[404]})
async def get_run(run_id: str, include_content: bool = Query(False, description="Include artifact bodies"),
                  ctx: Ctx = Depends(get_ctx)) -> RunView:
    return run_view(find_run(ctx, run_id), include_content)


async def decide(ctx: Ctx, run_id: str, approval_id: str, body: ApprovalDecision,
                 status: ApprovalStatus, wait: bool, timeout: float) -> JSONResponse:
    run = find_run(ctx, run_id)
    approval = run.approvals.get(approval_id)
    if approval is None:
        raise ApiError(404, "approval_not_found", f"unknown approval '{approval_id}' for run '{run_id}'")
    key = (run_id, approval_id)
    if approval.status != ApprovalStatus.PENDING or key in ctx.supervisor.resolving:
        raise ApiError(409, "approval_not_pending", f"approval '{approval_id}' is not pending")
    action = ctx.engine.approve if status == ApprovalStatus.APPROVED else ctx.engine.reject
    # engine.approve() resumes a paused run, i.e. it can take as long as the rest of the run,
    # so it runs as a background task exactly like POST /runs.
    ctx.supervisor.launch(run_id, action(run_id, approval_id, body.actor, body.comment), resolving=key)
    if wait:
        await ctx.supervisor.wait(run_id, timeout)
        return reply(run_view(find_run(ctx, run_id)), 200)
    return reply(ApprovalAccepted(run_id=run_id, approval_id=approval_id, decision=status, actor=body.actor,
                                  links=links_for(run_id)), 202)


APPROVAL_RESPONSES = {
    202: {"model": ApprovalAccepted, "description": "Decision accepted; the run resumes in the background"},
    200: {"model": RunView, "description": "With wait=true: the run once settled (or on timeout)"},
    **ERRORS,
}


@router.post("/runs/{run_id}/approvals/{approval_id}/approve", tags=["approvals"], response_model=None,
             status_code=202, responses=APPROVAL_RESPONSES)
async def approve(run_id: str, approval_id: str, body: ApprovalDecision, wait: bool = WaitParam,
                  timeout: float = TimeoutParam, ctx: Ctx = Depends(get_ctx)) -> JSONResponse:
    """Approve a pending approval. ``actor`` is mandatory: every decision is attributed."""
    return await decide(ctx, run_id, approval_id, body, ApprovalStatus.APPROVED, wait, timeout)


@router.post("/runs/{run_id}/approvals/{approval_id}/reject", tags=["approvals"], response_model=None,
             status_code=202, responses=APPROVAL_RESPONSES)
async def reject(run_id: str, approval_id: str, body: ApprovalDecision, wait: bool = WaitParam,
                 timeout: float = TimeoutParam, ctx: Ctx = Depends(get_ctx)) -> JSONResponse:
    """Reject a pending approval: the gated stage fails and its dependents are skipped."""
    return await decide(ctx, run_id, approval_id, body, ApprovalStatus.REJECTED, wait, timeout)


@router.post("/runs/{run_id}/stop", response_model=RunView, tags=["runs"],
             responses={404: ERRORS[404], 409: ERRORS[409]})
async def stop_run(run_id: str, ctx: Ctx = Depends(get_ctx)) -> RunView:
    """Safe-stop: in-flight stages finish, everything not started is skipped."""
    run = find_run(ctx, run_id)
    if run.state in TERMINAL_RUN_STATES:
        raise ApiError(409, "run_already_finished", f"run '{run_id}' is already {run.state.value}")
    return run_view(ctx.engine.stop(run_id))


@router.get("/runs/{run_id}/trace", response_model=TraceView, tags=["runs"], responses={404: ERRORS[404]})
async def get_trace(run_id: str, ctx: Ctx = Depends(get_ctx)) -> TraceView:
    """Audit events (telemetry) plus the decision lineage stored on the run."""
    run = find_run(ctx, run_id)
    return TraceView(run_id=run_id, events=ctx.telemetry.events_for(run_id), decisions=list(run.decisions))


@router.get("/runs/{run_id}/dag", response_model=DagView, tags=["runs"], responses={404: ERRORS[404]})
async def get_dag(run_id: str, ctx: Ctx = Depends(get_ctx)) -> DagView:
    """Nodes with their current state, and dependency edges (source runs before target)."""
    run = find_run(ctx, run_id)
    nodes = [DagNode(id=s.spec.id, title=s.spec.title, agent=s.spec.agent, state=s.state,
                     requires_approval=s.spec.requires_approval, approval_kind=s.spec.approval_kind,
                     join=s.spec.join) for s in run.stages.values()]
    edges = [DagEdge(source=dep, target=s.spec.id) for s in run.stages.values() for dep in s.spec.depends_on]
    return DagView(run_id=run_id, state=run.state, nodes=nodes, edges=edges)


@router.get("/metrics", tags=["system"], response_model=None,
            responses={200: {"description": "JSON snapshot, or Prometheus text with ?format=prometheus",
                             "content": {"application/json": {}, "text/plain": {}}}})
async def get_metrics(format: Literal["json", "prometheus"] = "json", ctx: Ctx = Depends(get_ctx)) -> Any:
    snapshot = ctx.telemetry.snapshot()
    if format == "prometheus":
        return PlainTextResponse(to_prometheus_text(snapshot), media_type="text/plain; version=0.0.4")
    return snapshot


# ============================================================================ app factories
def create_orchestrator_app(
    engine: Any,
    telemetry: TelemetrySink,
    scenarios: ScenarioSource,
    store: Optional[RunStore] = None,
    on_shutdown: Optional[Callable[[], None]] = None,
) -> FastAPI:
    """Build an isolated app around an engine. ``scenarios`` is a dict, or a
    callable returning one (rebuilt per request). ``store`` defaults to the
    engine's own store. On shutdown, running tasks are cancelled and
    ``engine.close()`` (then ``on_shutdown``) is called."""
    supervisor = Supervisor(engine, telemetry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await supervisor.shutdown()
        engine.close()
        if on_shutdown:
            on_shutdown()

    app = FastAPI(
        title="Agentic SDLC Orchestrator",
        version="1.0.0",
        description="Start scenario runs, approve gated stages, stop runs, and inspect their DAG, trace and metrics.",
        lifespan=lifespan,
    )
    app.state.ctx = Ctx(engine=engine, telemetry=telemetry, store=store or engine.store,
                        scenarios=scenarios, supervisor=supervisor)
    install_error_handlers(app)
    app.include_router(router)
    return app


def create_default_app(db_path: str = "data/orchestrator.db", audit_path: str = "data/audit.jsonl") -> FastAPI:
    """Wire the real components. Imports are lazy so this module loads without them."""
    import os

    from scenarios.registry import SCENARIOS

    from .agents import build_agent_registry
    from .engine import Engine
    from .store import SqliteRunStore
    from .telemetry import Telemetry

    for path in (db_path, audit_path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    store, telemetry = SqliteRunStore(db_path), Telemetry(audit_path=audit_path)
    engine = Engine(build_agent_registry(), store=store, telemetry=telemetry)

    def close() -> None:
        telemetry.close()
        store.close()

    return create_orchestrator_app(engine, telemetry, lambda: {n: build() for n, build in SCENARIOS.items()},
                                   on_shutdown=close)
