"""Policy engine: hard guardrails that agents and graph authors cannot skip.

Each rule is a small named function that raises ``PolicyViolation`` when it
objects. Rules live in two lists, so adding one is a one-line change.

A ``PolicyViolation`` is a HARD STOP: the engine never retries it, never falls
back, and safe-stops the rest of the run.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Iterator, Optional

from .models import Autonomy, StageSpec

HIGH_IMPACT = {"schema_change", "public_api_change", "production_release", "security_exception"}


class PolicyViolation(Exception):
    def __init__(self, rule: str, message: str):
        super().__init__(f"[{rule}] {message}")
        self.rule = rule
        self.message = message


# ---------------------------------------------------------------- helpers
Path = tuple[str, ...]  # dict keys and "[i]" list indices, from the artifact root down to one scalar


def _scalars(obj: Any) -> Iterator[tuple[Path, Any]]:
    """Yield (full key path, value) for every str / int / float / bool in a JSON-like structure.
    Iterative, so a deeply nested artifact cannot blow the stack."""
    stack: list[tuple[Path, Any]] = [((), obj)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            stack.extend(((*path, str(k)), v) for k, v in reversed(list(node.items())))
        elif isinstance(node, (list, tuple)):
            stack.extend(((*path, f"[{i}]"), v) for i, v in reversed(list(enumerate(node))))
        elif isinstance(node, (str, int, float, bool)):
            yield path, node


def _where(name: str, path: Path) -> str:
    """"artifact 'x' field 'a.b[0].c'": key names only, never values."""
    text = "".join(c if c.startswith("[") else f".{c}" for c in path).lstrip(".")
    return f"artifact '{name}'" + (f" field '{text}'" if text else "")


def _keys(path: Path) -> list[str]:
    """The dict-key components of a path (list indices dropped)."""
    return [c for c in path if not c.startswith("[")]


def _compact(component: str) -> str:
    """'Client-Secret' / 'clientSecret' / 'client_secret' -> 'clientsecret'."""
    return re.sub(r"[_\-. ]", "", component).lower()


def _snake(component: str) -> str:
    """'accessToken' -> 'access_token' (word boundaries kept, for names where substrings mislead)."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", component).lower()


# Only these exact whole values mean "not a real secret": ${NAME}, <redacted>, ***, REDACTED, or empty.
_PLACEHOLDER = re.compile(r"(?i)^(\$\{[A-Za-z_][A-Za-z0-9_]*\}|<redacted>|\*\*\*|redacted)?$")


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.match(value.strip()))


# ------------------------------------------------------- secret rules
# Each rule is (path, value) -> a short label of what was found, or None. Labels never contain the value.
_AWS_KEY_ID = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")
_AWS_SECRET_40 = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")
_PEM_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[\w-]{4,}\.eyJ[\w-]{4,}\.")
_AUTH_HEADER = re.compile(r"(?i)\bauthorization\s*[:=]\s*[\"']?(?:bearer|basic|digest|token)\s+([^\s\"']+)")
_CREDENTIAL_SCHEME = re.compile(r"(?is)^\s*(?:bearer|basic|digest|token)\s+(\S+)")

# `password = "x"`, `"api_key": "x"`, `DB_PASSWORD=x`, `token: x` inside source / config / JSON text.
_ASSIGN_NAME = (r"(?:password|passwd|pwd|secret|secret[_-]?key|private[_-]?key|api[_-]?key|access[_-]?key"
                r"|client[_-]?secret|token|credential)s?")
_ASSIGNMENT = re.compile(
    rf"""(?ix){_ASSIGN_NAME}["']?\s*(?P<sep>[:=])\s*
         (?:"(?P<dq>[^"\s]{{4,}})"|'(?P<sq>[^'\s]{{4,}})'|(?P<bare>[^\s"',;&)}}\]]{{6,}}))""")
_CODE_REFERENCE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")  # config.password / process.env.PW: code, not a value


def _known_token_formats(path: Path, value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if _AWS_KEY_ID.search(value):
        return "AWS access key id"
    if _PEM_PRIVATE_KEY.search(value):
        return "private key block"
    if _JWT.search(value):
        return "JWT-looking token"
    return None


def _aws_secret_access_key(path: Path, value: Any) -> Optional[str]:
    """A bare 40-char base64-ish string is only suspicious next to an 'aws' / 'secret' hint."""
    if not isinstance(value, str) or not re.search(r"(?i)aws|secret", "/".join(path) + " " + value):
        return None
    for token in _AWS_SECRET_40.findall(value):
        is_hex = re.fullmatch(r"[0-9a-fA-F]{40}", token)  # a git sha, not an AWS secret
        if not is_hex and re.search(r"[A-Za-z]", token) and re.search(r"[0-9/+]", token):
            return "AWS-style secret access key"
    return None


def _authorization_header(path: Path, value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if any(not _is_placeholder(m.group(1)) for m in _AUTH_HEADER.finditer(value)):
        return "credential in Authorization header"
    scheme = _CREDENTIAL_SCHEME.match(value)  # dict form: {'Authorization': 'Bearer x'}
    if scheme and not _is_placeholder(scheme.group(1)) and any("authorization" in _compact(c) for c in _keys(path)):
        return "credential in Authorization header"
    return None


def _assignment_in_text(path: Path, value: Any) -> Optional[str]:
    """Secret-named assignments inside a string. Quoted values: any non-placeholder literal. Unquoted values
    (env-file style) must be 6+ chars; with ':' they must also contain a non-letter, because 'token: required'
    is just prose."""
    if not isinstance(value, str):
        return None
    for m in _ASSIGNMENT.finditer(value):
        literal = m.group("dq") or m.group("sq")
        if literal is not None:
            if not _is_placeholder(literal):
                return "hard-coded credential literal"
            continue
        bare = m.group("bare")
        if _is_placeholder(bare) or _CODE_REFERENCE.match(bare) or re.search(r"[(\[]", bare):
            continue
        if m.group("sep") == "=" or not bare.isalpha():
            return "unquoted credential assignment"
    return None


# A field *named* like a secret. STRICT names: any non-placeholder value is a secret (numbers too).
_STRICT_KEY = re.compile(r"(?:password|passwd|pwd|secret|secretkey|privatekey|apikey|accesskey|clientsecret)s?$")
# BROAD names (substring of any path component): only long, space-free strings count.
_BROAD_KEY = re.compile(
    r"password|passwd|pwd|secret|privatekey|apikey|accesskey|auth(?!or|ority|enticat)|credential")
_TOKEN_WORD = re.compile(r"(?<![a-z])tokens?(?![a-z])")  # 'access_token' yes, 'tokenizer' no
_NOT_A_SECRET_SUFFIX = ("url", "uri", "endpoint", "type", "name", "method", "scheme", "header", "count", "length",
                        "ttl")
_PLAIN_URL = re.compile(r"(?i)^https?://[^/@\s]*(/\S*)?$")  # a URL without user:password@


def _secret_named_field(path: Path, value: Any) -> Optional[str]:
    parts = [_compact(c) for c in _keys(path)]
    if not parts:
        return None
    if any(_STRICT_KEY.search(p) for p in parts):
        if isinstance(value, str):
            return None if _is_placeholder(value) else "literal secret value"
        if isinstance(value, bool):  # `{"password": true}` yes, `{"has_password": true}` no
            return "literal secret value" if _STRICT_KEY.fullmatch(parts[-1]) else None
        return "literal secret value"  # int / float
    secret_like = any(_BROAD_KEY.search(p) for p in parts) or any(_TOKEN_WORD.search(_snake(c)) for c in _keys(path))
    broad = secret_like and not parts[-1].endswith(_NOT_A_SECRET_SUFFIX)
    if broad and isinstance(value, str) and len(value) >= 8 and not re.search(r"\s", value):
        if not _is_placeholder(value) and not _PLAIN_URL.match(value):
            return "long literal under a secret-like field"
    return None


SECRET_RULES: list[Callable[[Path, Any], Optional[str]]] = [
    _known_token_formats, _aws_secret_access_key, _authorization_header, _assignment_in_text, _secret_named_field,
]


def no_secrets(name: str, content: dict[str, Any]) -> None:
    """Reject credentials. The message names the field and the kind of secret, never the value."""
    for path, value in _scalars(content):
        for rule in SECRET_RULES:
            label = rule(path, value)
            if label:
                raise PolicyViolation("secret_detected", f"{label} found in {_where(name, path)}")


# ------------------------------------------------------- URL rules
_UNSAFE_SCHEMES = ("javascript:", "data:", "file:", "vbscript:", "blob:", "about:")
_URL_WORDS = {"url", "uri", "href", "link", "redirect", "next", "return", "destination", "target", "location",
              "src", "action", "callback"}
_URL_ATTRIBUTE = re.compile(r"""(?i)\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^>]*))""")


def _is_url_field(component: str) -> bool:
    """Any word of the key counts: 'target_url', 'redirectTo', 'Callbacks', 'next'."""
    words = re.split(r"[^a-z0-9]+", _snake(component))
    return any(w in _URL_WORDS or w.rstrip("s") in _URL_WORDS for w in words if w)


def _unsafe_scheme(text: str) -> Optional[str]:
    """Browsers ignore whitespace / control chars inside a scheme ('java<TAB>script:'), so do we."""
    compact = re.sub(r"[\x00-\x20\x7f-\x9f​-‏ - ﻿]", "", text).lower()
    return next((s for s in _UNSAFE_SCHEMES if compact.startswith(s)), None)


def safe_destination_urls(name: str, content: dict[str, Any]) -> None:
    """Reject script/data/file-style URLs: in any value under a URL-ish key path, and in href=/src= attributes."""
    for path, value in _scalars(content):
        if not isinstance(value, str):
            continue
        candidates = [value] if any(_is_url_field(c) for c in _keys(path)) else []
        candidates += [next(g for g in m.groups() if g is not None) for m in _URL_ATTRIBUTE.finditer(value)]
        for candidate in candidates:
            scheme = _unsafe_scheme(candidate)
            if scheme:
                raise PolicyViolation("unsafe_url_scheme", f"{_where(name, path)} uses forbidden scheme '{scheme}'")


# ----------------------------------------------------------- stage rules
def approval_kind_of(spec: StageSpec) -> Optional[str]:
    """Normalised approval kind (StageSpec already normalises; this also covers specs built without validation)."""
    return (spec.approval_kind or "").strip().lower() or None


def needs_human(spec: StageSpec) -> bool:
    return spec.requires_approval or spec.autonomy == Autonomy.HUMAN_GATED or approval_kind_of(spec) in HIGH_IMPACT


def high_impact_needs_approval(spec: StageSpec) -> None:
    kind = approval_kind_of(spec)
    if kind in HIGH_IMPACT and not spec.requires_approval:
        raise PolicyViolation(
            "high_impact_requires_approval",
            f"stage '{spec.id}' is '{kind}' (high impact) but requires_approval is False",
        )


def release_not_fully_autonomous(spec: StageSpec) -> None:
    looks_like_release = "release" in spec.id.lower() or "release" in spec.agent.lower()
    if (looks_like_release or approval_kind_of(spec) == "production_release") and spec.autonomy == Autonomy.FULL:
        raise PolicyViolation(
            "release_requires_human",
            f"release stage '{spec.id}' may not run with autonomy=full",
        )


def fallback_requires_own_approval(spec: StageSpec) -> None:
    """A human approved *this* agent. A fallback agent is a different action nobody approved.
    (A rollback agent may stay: undoing is the safe direction.)"""
    if spec.fallback_agent and needs_human(spec):
        raise PolicyViolation(
            "fallback_requires_own_approval",
            f"stage '{spec.id}' needs approval, so it may not define fallback_agent '{spec.fallback_agent}'",
        )


# ------------------------------------------------------- approval identity
# The engine's own machine actors: a human decision may never be attributed to them.
RESERVED_ACTORS = {"auto-approver", "demo-auto-approver", "engine", "system"}


def validate_human_actor(actor: Any, extra_reserved: Iterable[str] = ()) -> str:
    """Return the stripped actor name, or raise ValueError.

    NOT enforceable here: that the approver differs from whoever requested the change. The engine does not
    know who requested it; that needs an identity provider in front of the API."""
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a non-empty string")
    actor = actor.strip()
    if actor.lower() in RESERVED_ACTORS | {r.lower() for r in extra_reserved}:
        raise ValueError(f"actor '{actor}' is reserved for the engine and cannot make human decisions")
    return actor


ArtifactRule = Callable[[str, dict[str, Any]], None]
StageRule = Callable[[StageSpec], None]

ARTIFACT_RULES: list[ArtifactRule] = [no_secrets, safe_destination_urls]
STAGE_RULES: list[StageRule] = [high_impact_needs_approval, release_not_fully_autonomous,
                                fallback_requires_own_approval]


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
        return needs_human(spec)
