"""Policy engine: hard guardrails that agents and graph authors cannot skip.

Each rule is a small named function that raises ``PolicyViolation`` when it
objects. Rules live in two lists, so adding one is a one-line change.

A ``PolicyViolation`` is a HARD STOP: the engine never retries it, never falls
back, and safe-stops the rest of the run.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterator, Optional

from .models import Autonomy, StageSpec

HIGH_IMPACT = {"schema_change", "public_api_change", "production_release", "security_exception"}


class PolicyViolation(Exception):
    def __init__(self, rule: str, message: str):
        super().__init__(f"[{rule}] {message}")
        self.rule = rule
        self.message = message


# ---------------------------------------------------------------- helpers
def _leaves(obj: Any, key: Optional[str] = None) -> Iterator[tuple[Optional[str], str]]:
    """Yield (nearest dict key, string value) for every string in a JSON-like
    structure. List items inherit the key of the list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v, key)
    elif isinstance(obj, str):
        yield key, obj


# ------------------------------------------------------- artifact rules
# Patterns that are secrets wherever they appear in a string.
_SECRET_PATTERNS = [
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer token in Authorization header", re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
_NAME = r"(?:password|passwd|secret|token|api[_-]?key)"
# `password = "hunter22"`, `"api_key": "abc123"` inside source text / JSON strings.
_ASSIGNED_LITERAL = re.compile(rf"(?i){_NAME}[\"']?\s*[:=]\s*[\"']([^\"'\s]{{4,}})[\"']")
# A dict field literally named like a secret, e.g. {"client_secret": "abc"}.
_SECRET_FIELD = re.compile(rf"(?i){_NAME}$")
# Values that clearly are not a real secret (env refs, templates, masks).
_PLACEHOLDER = re.compile(r"(?i)^(\$.*|<.*>|\{\{.*\}\}|\*+|redacted|null|none)?$")


def no_secrets(name: str, content: dict[str, Any]) -> None:
    """Reject credentials. The message names the field, never echoes the value."""
    for key, text in _leaves(content):
        where = f"artifact '{name}'" + (f" field '{key}'" if key else "")
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                raise PolicyViolation("secret_detected", f"{label} found in {where}")
        for match in _ASSIGNED_LITERAL.finditer(text):
            if not _PLACEHOLDER.match(match.group(1)):
                raise PolicyViolation("secret_detected", f"hard-coded credential literal found in {where}")
        if key and _SECRET_FIELD.search(key) and not _PLACEHOLDER.match(text.strip()):
            raise PolicyViolation("secret_detected", f"{where} holds a literal secret value")


_UNSAFE_SCHEMES = ("javascript:", "data:", "file:")
_URL_FIELD = re.compile(r"(?i)(url|uri|href|link|destination|redirect|target)s?$")


def safe_destination_urls(name: str, content: dict[str, Any]) -> None:
    """Reject URL-ish fields that use a script/data/file scheme (open-redirect
    and XSS vectors for a URL shortener)."""
    for key, text in _leaves(content):
        if key and _URL_FIELD.search(key):
            # Browsers ignore whitespace/control chars inside a scheme ("java\nscript:").
            compact = re.sub(r"[\x00-\x20]", "", text).lower()
            if compact.startswith(_UNSAFE_SCHEMES):
                scheme = compact.split(":", 1)[0]
                raise PolicyViolation("unsafe_url_scheme",
                                      f"artifact '{name}' field '{key}' uses forbidden scheme '{scheme}:'")


# ----------------------------------------------------------- stage rules
def high_impact_needs_approval(spec: StageSpec) -> None:
    if spec.approval_kind in HIGH_IMPACT and not spec.requires_approval:
        raise PolicyViolation(
            "high_impact_requires_approval",
            f"stage '{spec.id}' is '{spec.approval_kind}' (high impact) but requires_approval is False",
        )


def release_not_fully_autonomous(spec: StageSpec) -> None:
    looks_like_release = "release" in spec.id.lower() or "release" in spec.agent.lower()
    if (looks_like_release or spec.approval_kind == "production_release") and spec.autonomy == Autonomy.FULL:
        raise PolicyViolation(
            "release_requires_human",
            f"release stage '{spec.id}' may not run with autonomy=full",
        )


ArtifactRule = Callable[[str, dict[str, Any]], None]
StageRule = Callable[[StageSpec], None]

ARTIFACT_RULES: list[ArtifactRule] = [no_secrets, safe_destination_urls]
STAGE_RULES: list[StageRule] = [high_impact_needs_approval, release_not_fully_autonomous]


class PolicyEngine:
    def __init__(
        self,
        artifact_rules: Optional[list[ArtifactRule]] = None,
        stage_rules: Optional[list[StageRule]] = None,
    ):
        self.artifact_rules = list(ARTIFACT_RULES if artifact_rules is None else artifact_rules)
        self.stage_rules = list(STAGE_RULES if stage_rules is None else stage_rules)

    def check_artifact(self, name: str, content: dict[str, Any]) -> None:
        """Raise PolicyViolation if the artifact breaks any rule."""
        for rule in self.artifact_rules:
            rule(name, content)

    def check_stage(self, spec: StageSpec) -> None:
        """Raise PolicyViolation if the stage definition breaks any rule."""
        for rule in self.stage_rules:
            rule(spec)

    def requires_human(self, spec: StageSpec) -> bool:
        """Must a person approve this stage before it runs?"""
        return (
            spec.requires_approval
            or spec.autonomy == Autonomy.HUMAN_GATED
            or spec.approval_kind in HIGH_IMPACT
        )
