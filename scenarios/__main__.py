"""Entry point: python -m scenarios ..."""
import warnings

# Importing fastapi.testclient (the agents drive the real shortener app with it) makes Starlette print a
# deprecation notice about httpx on every CLI command. Silence exactly that message, not warnings in general.
warnings.filterwarnings("ignore", message=r"Using `httpx` with `starlette\.testclient` is deprecated")

from .cli import main  # noqa: E402

raise SystemExit(main())
