"""Run the server: python -m shortener

Environment variables (all optional):
  SHORTENER_DATA_FILE  path of a JSON file to persist links in
  SHORTENER_IP_SALT    stable salt for visitor hashes (random per start if unset)
"""
import os

import uvicorn

from shortener.app import create_app
from shortener.store import UrlStore

PORT = 8080


def main() -> None:
    store = UrlStore(path=os.environ.get("SHORTENER_DATA_FILE"))
    uvicorn.run(create_app(store=store), host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
