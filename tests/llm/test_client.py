"""LLM client: JSON parsing, the Gemini REST client (httpx.MockTransport, no network) and key redaction."""
import json

import httpx
import pytest
from pydantic import BaseModel

from orchestrator.llm import LLMAuthError, LLMInvalidOutput, LLMRateLimited, LLMUnavailable, ScriptedLLM, build_client
from orchestrator.llm.client import parse_model_json, strip_fences
from orchestrator.llm.config import LLMSettings
from orchestrator.llm.gemini import GeminiClient

KEY = "AIzaSy-FAKE-KEY-0123456789abcdef"


class Out(BaseModel):
    name: str
    n: int


def answer(text: str, finish: str = "STOP") -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]})


def client(handler, sleeps=None, models=("m1", "m2", "m3")) -> GeminiClient:
    return GeminiClient(KEY, models[0], models[1:], transport=httpx.MockTransport(handler),
                        sleep=(sleeps.append if sleeps is not None else lambda s: None))


def ask(c: GeminiClient) -> Out:
    return c.generate_json("system", "prompt", Out)


# --------------------------------------------------------------- parsing
@pytest.mark.parametrize("text", ['{"name": "a", "n": 1}', '```json\n{"name": "a", "n": 1}\n```',
                                  '```\n{"name": "a", "n": 1}\n```', '  {"name": "a", "n": 1}  '])
def test_parse_accepts_plain_and_fenced_json(text):
    assert parse_model_json(text, Out) == Out(name="a", n=1)


def test_strip_fences_leaves_plain_text_alone():
    assert strip_fences("hello") == "hello"


def test_parse_rejects_bad_json_with_a_short_message():
    with pytest.raises(LLMInvalidOutput, match="not valid JSON"):
        parse_model_json("sure! here you go {", Out)


def test_parse_rejects_wrong_schema_without_echoing_the_value():
    with pytest.raises(LLMInvalidOutput) as err:
        parse_model_json('{"name": "password=hunter2hunter2", "n": "many"}', Out)
    assert "n:" in str(err.value) and "hunter2" not in str(err.value) and len(str(err.value)) <= 300


def test_parse_rejects_non_object_json():
    with pytest.raises(LLMInvalidOutput):
        parse_model_json("[1, 2]", Out)


# ------------------------------------------------------- scripted fake
def test_scripted_llm_validates_like_a_real_model_and_records_prompts():
    fake = ScriptedLLM([{"name": "a", "n": 1}, {"name": "a"}, RuntimeError("boom")])
    assert fake.generate_json("sys", "p1", Out).n == 1
    with pytest.raises(LLMInvalidOutput):
        fake.generate_json("sys", "p2", Out)
    with pytest.raises(RuntimeError):
        fake.generate_json("sys", "p3", Out)
    with pytest.raises(LLMUnavailable):
        fake.generate_json("sys", "p4", Out)
    assert [c["prompt"] for c in fake.calls] == ["p1", "p2", "p3", "p4"]


def test_scripted_llm_can_repeat_its_last_answer():
    fake = ScriptedLLM([{"name": "a", "n": 1}], repeat_last=True)
    assert [fake.generate_json("s", "p", Out).n for _ in range(3)] == [1, 1, 1]


# --------------------------------------------------------------- Gemini
def test_gemini_success_sends_key_in_header_only():
    seen = []

    def handler(request):
        seen.append(request)
        return answer('```json\n{"name": "ok", "n": 2}\n```')

    out = ask(client(handler))
    assert out == Out(name="ok", n=2)
    request = seen[0]
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url) and "key=" not in str(request.url)
    assert str(request.url).endswith("/models/m1:generateContent")
    body = json.loads(request.content)
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json" and config["temperature"] == 0.2
    assert config["thinkingConfig"] == {"thinkingLevel": "minimal"} and config["maxOutputTokens"] >= 1024
    assert body["systemInstruction"]["parts"][0]["text"] == "system"


def test_gemini_ignores_thought_parts():
    def handler(request):
        parts = [{"text": "thinking out loud", "thought": True}, {"text": '{"name": "a", "n": 1}'}]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]})

    assert ask(client(handler)).n == 1


@pytest.mark.parametrize("response, error", [
    (httpx.Response(200, json={"candidates": []}), LLMUnavailable),
    (httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), LLMUnavailable),
    (answer("", "STOP"), LLMInvalidOutput),
    (answer("", "SAFETY"), LLMUnavailable),
    (answer('{"name": "a"', "MAX_TOKENS"), LLMInvalidOutput),
    (answer("not json at all"), LLMInvalidOutput),
    (answer('{"name": "a"}'), LLMInvalidOutput),
    (httpx.Response(200, text="<html>"), LLMInvalidOutput),
    (httpx.Response(400, json={"error": {"message": "bad request"}}), LLMUnavailable),
    (httpx.Response(403, json={"error": {"message": "forbidden"}}), LLMUnavailable),
])
def test_gemini_bad_answers_map_to_llm_errors(response, error):
    with pytest.raises(error):
        ask(client(lambda request: response))


def test_gemini_retries_429_honouring_retry_after():
    sleeps, replies = [], [httpx.Response(429, headers={"Retry-After": "3"}), answer('{"name": "a", "n": 1}')]
    assert ask(client(lambda request: replies.pop(0), sleeps)).n == 1
    assert sleeps == [3.0]


def test_gemini_retry_is_bounded_and_falls_over_to_the_next_model():
    sleeps, urls = [], []

    def handler(request):
        urls.append(request.url.path.split("/")[-1])
        if "m3" in urls[-1]:
            return answer('{"name": "third", "n": 3}')
        return httpx.Response(429, headers={"Retry-After": "1"})

    c = client(handler, sleeps)
    assert ask(c).name == "third" and c.model == "m3"
    assert urls.count("m1:generateContent") == 3 and urls.count("m2:generateContent") == 3  # 1 try + 2 retries
    assert len(sleeps) == 4


def test_gemini_gives_up_with_rate_limited_error():
    with pytest.raises(LLMRateLimited) as err:
        ask(client(lambda request: httpx.Response(429, headers={"Retry-After": "2"}), []))
    assert err.value.retry_after == 2.0


def test_gemini_does_not_wait_for_a_very_long_retry_after():
    sleeps = []
    with pytest.raises(LLMRateLimited):
        ask(client(lambda request: httpx.Response(429, headers={"Retry-After": "600"}), sleeps))
    assert sleeps == []


def test_gemini_unknown_model_hands_over_to_the_next():
    def handler(request):
        if "m1:" in str(request.url):
            return httpx.Response(404, json={"error": {"message": "no such model"}})
        return answer('{"name": "second", "n": 2}')

    c = client(handler)
    assert ask(c).name == "second" and c.model == "m2"


def test_gemini_5xx_is_retried_then_reported():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(LLMUnavailable, match="503"):
        ask(client(handler, models=("only",)))
    assert len(calls) == 3


def test_gemini_network_errors_become_llm_unavailable():
    def handler(request):
        raise httpx.ConnectError("dns failure for " + KEY)

    with pytest.raises(LLMUnavailable) as err:
        ask(client(handler))
    assert KEY not in str(err.value) and "ConnectError" in str(err.value)


def test_gemini_timeout_becomes_llm_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMUnavailable, match="timed out"):
        ask(client(handler, models=("only",)))


def test_key_never_appears_in_errors_even_if_the_provider_echoes_it():
    bodies = [httpx.Response(400, json={"error": {"message": f"API key not valid: {KEY}"}}),
              httpx.Response(500, text=f"oops {KEY}"), answer(f"nonsense {KEY}"),
              httpx.Response(200, text=f"{KEY}")]
    for body in bodies:
        with pytest.raises(Exception) as err:
            ask(client(lambda request, b=body: b, models=("only",)))
        assert KEY not in str(err.value) and KEY not in repr(err.value)
        assert KEY not in "".join(str(x) for x in (err.value.__cause__, err.value.__context__))


def test_client_and_settings_do_not_leak_the_key_in_repr():
    c = client(lambda request: answer("{}"))
    assert KEY not in repr(c) and KEY not in repr(vars(c).get("_http"))
    assert KEY not in repr(LLMSettings(provider="gemini", api_key=KEY))


def test_gemini_requires_a_key():
    with pytest.raises(LLMUnavailable):
        GeminiClient("")


def test_build_client_without_configuration_is_llm_unavailable():
    with pytest.raises(LLMUnavailable, match="not configured"):
        build_client(LLMSettings(provider="none"))


# ------------------------------------------------- a refused key is permanent
class Counting:
    """An httpx handler that counts requests and always answers with ``response``."""

    def __init__(self, response):
        self.response, self.calls = response, 0

    def __call__(self, request):
        self.calls += 1
        return self.response


REFUSALS = [httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}}),
            httpx.Response(401, json={"error": {"message": "unauthenticated"}}),
            httpx.Response(403, json={"error": {"message": "permission denied"}})]


@pytest.mark.parametrize("refusal", REFUSALS, ids=["400-key-not-valid", "401", "403"])
def test_a_refused_key_stops_at_the_first_model_and_opens_the_circuit(refusal):
    handler = Counting(refusal)
    c = client(handler)                                  # three models configured
    with pytest.raises(LLMAuthError):
        ask(c)
    assert handler.calls == 1                            # the other two models were not tried
    for _ in range(5):                                   # circuit open: no network request at all
        with pytest.raises(LLMUnavailable, match="refused earlier"):
            ask(c)
    assert handler.calls == 1


def test_a_refused_key_error_never_contains_the_key_and_the_circuit_message_does_not_grow():
    key_echo = httpx.Response(400, json={"error": {"message": f"API key not valid: {KEY}"}})
    c = client(Counting(key_echo))
    messages = []
    for _ in range(3):
        with pytest.raises(LLMAuthError) as err:
            ask(c)
        messages.append(str(err.value))
    assert all(KEY not in m for m in messages) and "<redacted>" in messages[0]
    assert messages[1] == messages[2] and messages[1].count("refused earlier") == 1


def test_other_client_errors_are_not_treated_as_a_refused_key():
    handler = Counting(httpx.Response(400, json={"error": {"message": "Invalid JSON payload"}}))
    c = client(handler)
    for _ in range(2):
        with pytest.raises(LLMUnavailable) as err:
            ask(c)
        assert not isinstance(err.value, LLMAuthError)
    assert handler.calls == 6                            # 3 models x 2 calls: no circuit, the next model is tried


def test_a_transient_failure_does_not_open_the_circuit():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404) if len(calls) == 1 else answer('{"name": "a", "n": 1}')

    c = client(handler)
    assert ask(c).name == "a" and len(calls) == 2        # m1 missing -> m2 answered
    assert ask(c).name == "a"                            # still usable afterwards


def test_each_client_has_its_own_circuit():
    refused, healthy = client(Counting(REFUSALS[1])), client(lambda request: answer('{"name": "a", "n": 1}'))
    with pytest.raises(LLMAuthError):
        ask(refused)
    assert ask(healthy).n == 1
