"""Tests are offline and deterministic by default, whatever the developer's .env says."""
import os

os.environ["LLM_ENABLED"] = "0"
