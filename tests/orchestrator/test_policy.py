import pytest

from orchestrator.models import Autonomy, StageSpec
from orchestrator.policy import HIGH_IMPACT, PolicyEngine, PolicyViolation

policy = PolicyEngine()


def spec(**kw):
    kw.setdefault("id", "s")
    return StageSpec(title="t", agent=kw.pop("agent", "a"), **kw)


@pytest.mark.parametrize("content", [
    {"env": "AWS_KEY=AKIAIOSFODNN7EXAMPLE"},
    {"code": 'password = "hunter2xyz"'},
    {"code": "api_key: 'abcd1234efgh'"},
    {"json": '{"token": "abcdef123456"}'},
    {"password": "hunter2xyz"},
    {"nested": {"client_secret": "s3cr3tvalue"}},
    {"items": [{"access_token": "abc123456"}]},
    {"headers": "Authorization: Bearer eyJhbGciOi.payload.sig"},
    {"key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."},
])
def test_secrets_are_rejected(content):
    with pytest.raises(PolicyViolation) as exc:
        policy.check_artifact("implementation", content)
    assert exc.value.rule == "secret_detected"


def test_violation_message_never_echoes_the_secret():
    with pytest.raises(PolicyViolation) as exc:
        policy.check_artifact("cfg", {"password": "hunter2xyz"})
    assert "hunter2xyz" not in str(exc.value)
    assert "cfg" in exc.value.message and "password" in exc.value.message


@pytest.mark.parametrize("content", [
    {"text": "hello world", "count": 3},
    {"password": "${DB_PASSWORD}"},
    {"password": ""},
    {"token": "<redacted>"},
    {"note": "tokens are issued by the auth service", "tokenizer": "x"},
    {"code": "password = os.environ['PW']"},
    {"short_code": "abc123", "url": "https://example.com/path"},
])
def test_clean_content_is_accepted(content):
    policy.check_artifact("a", content)


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\nscript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=", "file:///etc/passwd",
])
def test_unsafe_destination_schemes_rejected(url):
    with pytest.raises(PolicyViolation) as exc:
        policy.check_artifact("api", {"destination": url})
    assert exc.value.rule == "unsafe_url_scheme"


def test_unsafe_scheme_in_nested_or_listed_url_field():
    with pytest.raises(PolicyViolation):
        policy.check_artifact("api", {"requests": [{"target_url": "javascript:x"}]})
    with pytest.raises(PolicyViolation):
        policy.check_artifact("api", {"urls": ["https://ok.example", "file:///x"]})


def test_http_urls_allowed():
    policy.check_artifact("api", {"destination": "https://example.com", "url": "http://a.b/c?d=javascript:"})


@pytest.mark.parametrize("kind", sorted(HIGH_IMPACT))
def test_high_impact_stage_must_require_approval(kind):
    with pytest.raises(PolicyViolation) as exc:
        policy.check_stage(spec(approval_kind=kind, requires_approval=False))
    assert exc.value.rule in ("high_impact_requires_approval", "release_requires_human")
    if kind != "production_release":  # that one is also caught by the release rule when autonomy=full
        policy.check_stage(spec(approval_kind=kind, requires_approval=True))


@pytest.mark.parametrize("kwargs", [
    {"id": "release"},
    {"id": "ship", "agent": "release_agent"},
    {"id": "go_live", "approval_kind": "production_release", "requires_approval": True},
])
def test_release_stage_cannot_be_fully_autonomous(kwargs):
    with pytest.raises(PolicyViolation) as exc:
        policy.check_stage(spec(autonomy=Autonomy.FULL, **kwargs))
    assert exc.value.rule == "release_requires_human"


def test_release_stage_ok_when_supervised_and_gated():
    policy.check_stage(spec(id="release", autonomy=Autonomy.SUPERVISED,
                            approval_kind="production_release", requires_approval=True))
    policy.check_stage(spec(id="build", autonomy=Autonomy.FULL))


def test_requires_human():
    assert not policy.requires_human(spec())
    assert policy.requires_human(spec(requires_approval=True))
    assert policy.requires_human(spec(autonomy=Autonomy.HUMAN_GATED))
    assert policy.requires_human(spec(approval_kind="schema_change"))


def test_rules_are_pluggable():
    def no_foo(name, content):
        if "foo" in content:
            raise PolicyViolation("no_foo", "foo is banned")
    custom = PolicyEngine(artifact_rules=[no_foo], stage_rules=[])
    custom.check_artifact("x", {"password": "hunter2xyz"})  # default rules replaced
    with pytest.raises(PolicyViolation, match="foo is banned"):
        custom.check_artifact("x", {"foo": 1})
