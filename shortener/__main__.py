"""Run the server: python -m shortener

Environment variables (all optional):
  SHORTENER_DATA_FILE  path of a JSON file to persist links in
  SHORTENER_IP_SALT    stable salt for visitor hashes (random per start if unset)
  HOST, PORT           bind address (default 127.0.0.1:8080; containers use 0.0.0.0)
"""
import os

import uvicorn

from shortener.app import create_app
from shortener.store import UrlStore


def main() -> None:
    store = UrlStore(path=os.environ.get("SHORTENER_DATA_FILE"))
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(create_app(store=store), host=host, port=port)


if __name__ == "__main__":
    main()
