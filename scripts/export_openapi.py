"""Write the OpenAPI specs of both services to docs/.

    python scripts/export_openapi.py

The orchestrator app is built around inert stubs (nothing is called while the
schema is generated), so no agents, database or scenarios are needed.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.api import create_orchestrator_app  # noqa: E402
from shortener.app import create_app  # noqa: E402


class _Stub:
    """Stands in for the engine and telemetry: only used to build the schema."""

    store = None

    def close(self) -> None:
        pass


def write(name: str, schema: dict) -> None:
    path = ROOT / "docs" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)} ({len(schema['paths'])} paths)")


def main() -> None:
    write("openapi.shortener.json", create_app().openapi())
    stub = _Stub()
    write("openapi.orchestrator.json", create_orchestrator_app(stub, stub, {}).openapi())


if __name__ == "__main__":
    main()
