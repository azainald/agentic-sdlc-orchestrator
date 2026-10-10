"""Run the orchestrator API: python -m orchestrator

Runs persist in SQLite (data/orchestrator.db); the audit trail is appended to
data/audit.jsonl. Browse the API at http://127.0.0.1:8090/docs
"""
import os

import uvicorn

from orchestrator.api import create_default_app


def main() -> None:
    # Binds to loopback by default; containers set HOST=0.0.0.0 explicitly.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(create_default_app(), host=host, port=port)


if __name__ == "__main__":
    main()
