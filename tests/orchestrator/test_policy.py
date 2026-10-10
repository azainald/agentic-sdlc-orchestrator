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


# ======================================================== hardened artifact scan
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # AWS docs example: 40 chars of base64-ish


# (content, the secret fragment that must never appear in the violation message)
SECRET_BYPASSES = [
    # numbers / bools under secret-named keys
    ({"db": {"password": 12345678}}, "12345678"),
    ({"api_key": 99887766}, "99887766"),
    ({"client_secret": 1.5}, None),
    ({"password": True}, None),
    # full key path: deep dicts and lists
    ({"users": [{"profile": {"password": "x1y2z3"}}]}, "x1y2z3"),
    ({"credentials": {"value": "abcdefgh1234"}}, "abcdefgh1234"),
    ({"tokens": ["abcdefgh1234"]}, "abcdefgh1234"),
    ({"a": {"b": {"c": {"d": {"secret": "deepvalue"}}}}}, "deepvalue"),
    # key-name variants (substring on any path component, case-insensitive)
    ({"X-Api-Key": "abc123"}, "abc123"),
    ({"apiKey": "abc123"}, "abc123"),
    ({"SECRET_KEY": "abc123"}, "abc123"),
    ({"privateKey": "abc123"}, "abc123"),
    ({"access_key": "abc123"}, "abc123"),
    ({"DB_PWD": "abc123"}, "abc123"),
    ({"passwd": "abc123"}, "abc123"),
    # Authorization in dict form and in text
    ({"Authorization": "Bearer abc123"}, "abc123"),
    ({"headers": {"authorization": "Basic dXNlcjpwYXNz"}}, "dXNlcjpwYXNz"),
    ({"curl": "curl -H 'Authorization: Bearer abc123def' https://x"}, "abc123def"),
    # unquoted assignments inside strings
    ({"env": "DB_PASSWORD=hunter2xyz"}, "hunter2xyz"),
    ({"env": "password: hunter2xyz"}, "hunter2xyz"),
    ({"env": "API_KEY=abcdefgh"}, "abcdefgh"),
    ({"env": "token=abcdef12345"}, "abcdef12345"),
    ({"env": "CLIENT_SECRET: s3cr3t!!"}, "s3cr3t!!"),
    ({"env": "aws_secret_access_key=" + AWS_SECRET}, AWS_SECRET),
    # well-known token formats, anywhere in any string
    ({"log": "key id AKIAIOSFODNN7EXAMPLE was used"}, "AKIAIOSFODNN7EXAMPLE"),
    ({"log": "X_AKIAIOSFODNN7EXAMPLE_"}, "AKIAIOSFODNN7EXAMPLE"),
    ({"log": "temporary ASIAIOSFODNN7EXAMPLE"}, "ASIAIOSFODNN7EXAMPLE"),
    ({"note": "use " + AWS_SECRET + " for the aws account"}, AWS_SECRET),
    ({"aws": {"value": AWS_SECRET}}, AWS_SECRET),
    ({"pem": "-----BEGIN PRIVATE KEY-----\nMIIE"}, "MIIE"),
    ({"pem": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl"}, "b3Bl"),
    ({"pem": "-----BEGIN EC PRIVATE KEY-----\nMHcC"}, "MHcC"),
    ({"jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig"}, "eyJzdWIiOiIxMjMifQ"),
    # placeholder look-alikes are NOT placeholders (only the exact whole values are)
    ({"password": "$ecretPa55"}, "ecretPa55"),
    ({"password": "<hunter2xyz>"}, "hunter2xyz"),
    ({"password": "${A} and more"}, "and more"),
    ({"password": "none"}, None),
    ({"password": "*"}, None),
    ({"env": "password=$ecretPa55"}, "ecretPa55"),
]


@pytest.mark.parametrize("content, fragment", SECRET_BYPASSES)
def test_secret_bypasses_are_rejected_without_echoing_the_secret(content, fragment):
    with pytest.raises(PolicyViolation) as exc:
        policy.check_artifact("cfg", content)
    assert exc.value.rule == "secret_detected"
    assert "cfg" in exc.value.message
    if fragment:
        assert fragment not in str(exc.value)


def test_violation_message_names_the_full_key_path():
    with pytest.raises(PolicyViolation) as exc:
        policy.check_artifact("cfg", {"users": [{"profile": {"password": "x1y2z3"}}]})
    assert "users[0].profile.password" in exc.value.message


CLEAN_SECRET_LOOKALIKES = [
    {"password": "${DB_PASSWORD}"}, {"password": "${db_password}"}, {"password": ""}, {"password": "   "},
    {"password": "<redacted>"}, {"password": "<REDACTED>"}, {"password": "***"}, {"password": "REDACTED"},
    {"api_key": "redacted"}, {"password": None},
    # names that merely contain a secret-ish word, with non-secret values
    {"token_count": 5}, {"max_tokens": 4096}, {"password_length": 12}, {"has_password": True},
    {"secret_rotation_days": 30}, {"tokenizer": "wordpiece"}, {"tokens": ["hello", "world"]},
    {"author": "Jane Q. Developer"}, {"authority": "example-authority"}, {"authorization_required": True},
    {"token_endpoint": "https://auth.example.com/oauth/token"}, {"token_type": "bearer"},
    {"auth_method": "client_secret_basic"}, {"auth": {"type": "oauth2"}}, {"credentials_required": True},
    # prose and code that talks about secrets
    {"note": "Passwords must never be logged; tokens are short-lived and the secret santa list is private."},
    {"doc": "password: required"}, {"doc": "token: required"},
    {"code": "password = os.environ['PW']"}, {"code": "password = config.password"},
    {"code": "token = get_token()"}, {"code": 'password = "${DB_PASSWORD}"'}, {"code": "api_key = '<redacted>'"},
    {"header": "Authorization: Bearer ${API_TOKEN}"},
    # looks random but has no aws/secret hint, or is a git sha next to the word 'secret'
    {"id": AWS_SECRET},
    {"note": "secret rotation commit da39a3ee5e6b4b0d3255bfef95601890afd80709"},
    {"jwt_docs": "a JWT starts with eyJ and has three dot-separated parts"},
    {"text": "hello world", "count": 3},
]


@pytest.mark.parametrize("content", CLEAN_SECRET_LOOKALIKES)
def test_normal_content_is_not_flagged_as_a_secret(content):
    policy.check_artifact("a", content)


# ======================================================== hardened URL scan
UNSAFE_URLS = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\tscript:alert(1)", "  \njavascript:x",
               "\x01javascript:x", "vbscript:msgbox(1)", "blob:https://x.example/uuid", "about:blank",
               "data:text/html;base64,PHNjcmlwdD4=", "file:///etc/passwd"]
URL_KEYS = ["url", "uri", "href", "link", "redirect", "redirect_to", "redirectTo", "next", "return", "return_url",
            "destination", "target", "location", "src", "action", "callback", "Callback_URL", "links"]


@pytest.mark.parametrize("url", UNSAFE_URLS)
@pytest.mark.parametrize("key", URL_KEYS)
def test_unsafe_schemes_rejected_under_every_url_like_key(key, url):
    with pytest.raises(PolicyViolation) as exc:
        policy.check_artifact("api", {key: url})
    assert exc.value.rule == "unsafe_url_scheme"


@pytest.mark.parametrize("content", [
    {"routes": [{"rules": {"callback": "javascript:x"}}]},
    {"redirect": {"to": "javascript:x"}},                 # URL-ish word on an outer path component
    {"links": ["https://ok.example", "vbscript:x"]},
    {"a": {"b": {"c": {"next": "blob:x"}}}},
])
def test_unsafe_scheme_found_anywhere_below_a_url_like_key(content):
    with pytest.raises(PolicyViolation) as exc:
        policy.check_artifact("api", content)
    assert exc.value.rule == "unsafe_url_scheme"


@pytest.mark.parametrize("html", [
    '<a href="javascript:alert(1)">x</a>', "<img src='data:image/svg+xml;base64,AAA'>",
    "<a href=javascript:alert(1)>x</a>", '<a href="java\tscript:alert(1)">x</a>', 'HREF = "vbscript:x"',
    '<iframe src="file:///etc/passwd">',
])
def test_unsafe_schemes_rejected_in_href_and_src_attributes_inside_strings(html):
    with pytest.raises(PolicyViolation) as exc:
        policy.check_artifact("docs", {"body": html})
    assert exc.value.rule == "unsafe_url_scheme"


@pytest.mark.parametrize("content", [
    {"url": "https://example.com", "href": "/relative/path", "link": "mailto:a@b.example"},
    {"target": "javascript", "next": "about us", "action": "file a report"},   # no scheme colon
    {"description": "never use javascript: URLs"},                              # not a URL field
    {"data": "data: some prose", "body": '<a href="https://ok.example">x</a>'},
    {"url": "http://a.b/c?d=javascript:"},
])
def test_safe_urls_and_prose_are_accepted(content):
    policy.check_artifact("api", content)


# ======================================================== new stage rules
def test_stage_needing_approval_may_not_define_a_fallback_agent():
    for kwargs in ({"requires_approval": True}, {"autonomy": Autonomy.HUMAN_GATED},
                   {"approval_kind": "schema_change", "requires_approval": True}):
        with pytest.raises(PolicyViolation) as exc:
            policy.check_stage(spec(fallback_agent="other", **kwargs))
        assert exc.value.rule == "fallback_requires_own_approval"
    policy.check_stage(spec(requires_approval=True, rollback_agent="undo"))  # undo is the safe direction
    policy.check_stage(spec(fallback_agent="other"))                         # no approval, no problem


def test_real_scenarios_satisfy_every_stage_rule():
    from scenarios.registry import SCENARIOS
    for build in SCENARIOS.values():
        for stage in build().graph:
            policy.check_stage(stage)


def test_approval_kind_is_normalised_on_the_spec_and_in_the_rules():
    assert spec(approval_kind="  Production_Release ").approval_kind == "production_release"
    assert spec(approval_kind="   ").approval_kind is None
    # a spec that skipped validation (model_construct) is still judged on the normalised kind
    raw = StageSpec.model_construct(id="s", title="t", agent="a", approval_kind=" SCHEMA_CHANGE ",
                                    requires_approval=False, autonomy=Autonomy.SUPERVISED, fallback_agent=None)
    with pytest.raises(PolicyViolation):
        policy.check_stage(raw)
    assert policy.requires_human(raw)
    release = StageSpec.model_construct(id="go", title="t", agent="a", approval_kind="Production_Release",
                                        requires_approval=True, autonomy=Autonomy.FULL, fallback_agent=None)
    with pytest.raises(PolicyViolation) as exc:
        policy.check_stage(release)
    assert exc.value.rule == "release_requires_human"


@pytest.mark.parametrize("actor, ok", [("alice", True), ("  alice  ", True), ("", False), ("   ", False),
                                       ("auto-approver", False), ("Demo-Auto-Approver", False),
                                       ("ENGINE", False), ("system", False), (None, False), (7, False)])
def test_validate_human_actor(actor, ok):
    from orchestrator.policy import validate_human_actor
    if ok:
        assert validate_human_actor(actor) == "alice"
    else:
        with pytest.raises(ValueError):
            validate_human_actor(actor)
