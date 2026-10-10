"""Run the orchestrator API: python -m orchestrator

Runs persist in SQLite (data/orchestrator.db); the audit trail is appended to
data/audit.jsonl. Browse the API at http://127.0.0.1:8090/docs
"""
import uvicorn

from orchestrator.api import create_default_app

PORT = 8090


def main() -> None:
    uvicorn.run(create_default_app(), host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
