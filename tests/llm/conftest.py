"""The documentation agent writes into docs/generated/<run_id>; remove what these tests created."""
import shutil

import pytest

from orchestrator.agents.common import PROJECT_ROOT

GENERATED = PROJECT_ROOT / "docs" / "generated"


@pytest.fixture(scope="session", autouse=True)
def _clean_generated_docs():
    before = set(GENERATED.glob("*")) if GENERATED.exists() else set()
    yield
    for path in set(GENERATED.glob("*")) - before:
        shutil.rmtree(path, ignore_errors=True)
