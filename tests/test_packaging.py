"""Deployment files: text-level checks (no Docker daemon needed, and none was used to build the image)."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_compose_publishes_the_unauthenticated_api_on_loopback_only():
    compose = text("docker-compose.yml")
    ports = re.findall(r'^\s*-\s*"([^"]+)"', compose, re.M)
    assert sorted(ports) == ["127.0.0.1:8080:8080", "127.0.0.1:8090:8090"]
    assert compose.count("no authentication - loopback only") >= 2
    assert "NOTE" not in compose and "hard-code" not in compose          # the stale remark about __main__ is gone


def test_the_entrypoints_really_read_host_and_port():
    for module in ("orchestrator", "shortener"):
        source = text(f"{module}/__main__.py")
        assert 'os.environ.get("HOST"' in source and 'os.environ.get("PORT"' in source


def test_the_dockerfile_creates_a_data_dir_owned_by_the_non_root_user():
    dockerfile = text("Dockerfile")
    assert "mkdir -p /app/data" in dockerfile and "chown -R app:app /app" in dockerfile
    assert dockerfile.index("chown -R app:app /app") < dockerfile.index("USER app")
    assert "hard-code" not in dockerfile


def test_the_data_directory_is_tracked_by_a_placeholder_only():
    assert (ROOT / "data" / ".gitkeep").is_file()
    for name in (".gitignore", ".dockerignore"):
        lines = [line.strip() for line in text(name).splitlines()]
        assert "data/*" in lines and lines.index("!data/.gitkeep") > lines.index("data/*")
    def git_ignores(path):  # 0 = ignored, 1 = not ignored, 128 = not a git checkout
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode

    if git_ignores("data/runs.db") == 128:
        pytest.skip("not a git checkout")
    assert git_ignores("data/runs.db") == 0 and git_ignores("data/audit.jsonl") == 0 and git_ignores("data/x.json") == 0
    assert git_ignores("data/.gitkeep") == 1


def test_ci_token_is_read_only():
    ci = text(".github/workflows/ci.yml")
    assert re.search(r"^permissions:\s*\n\s+contents: read\s*$", ci, re.M)


def test_lock_file_pins_exactly_the_direct_dependencies():
    names = lambda lines: sorted(re.split(r"[<>=]", line)[0].lower() for line in lines if line.strip())  # noqa: E731
    direct = text("requirements.txt").splitlines()
    pinned = text("requirements-lock.txt").splitlines()
    assert names(pinned) == names(direct)
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==\d+(\.\d+)*", line) for line in pinned if line.strip())
