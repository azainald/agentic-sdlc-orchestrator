"""Test helpers: an engine over an in-memory store with a no-op backoff, and a scenario starter."""
from orchestrator.agents import build_agent_registry
from orchestrator.engine import Engine
from orchestrator.store import InMemoryRunStore
from scenarios.registry import get_scenario


class Sleeper:
    """Replaces asyncio.sleep so retry backoff costs nothing; remembers the requested delays."""

    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


def make_engine(store=None, telemetry=None, sleeper=None) -> Engine:
    return Engine(build_agent_registry(), store=store if store is not None else InMemoryRunStore(),
                  telemetry=telemetry, sleep=sleeper or Sleeper())


def create(engine: Engine, name: str, graph=None, **params):
    """Create a run of a scenario. The pytest subprocess is skipped unless the test says otherwise."""
    scenario = get_scenario(name)
    return engine.create_run(scenario.name, scenario.requirement, graph or scenario.graph,
                             {"skip_pytest": True, **params})


async def start(engine: Engine, name: str, **params):
    return await engine.run(create(engine, name, **params).id)


def pending(run):
    return [a for a in run.approvals.values() if a.status.value == "pending"]
