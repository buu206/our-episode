"""Offline adapter tests. MockTransport never sends requests to the supplier."""

import asyncio
import json

import httpx
import pytest

from app.services.director import CloudDirector, DirectorError, parse_result


ASSETS = {"asset_a": 20.0, "asset_b": 5.0}
EVIDENCE = [{"asset_id": "asset_a", "start": 2.0, "end": 8.0, "note": "用户核对：一位参与者举杯。"}]
CONFIG = {
    "MODEL_API_KEY": "test-key",
    "MODEL_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "MODEL_NAME": "qwen3-vl-plus",
    "CLOUD_ENABLED": True,
    "CLOUD_RETENTION_VERIFIED": True,
    "CLOUD_REGION_VERIFIED": True,
}


def payload():
    return {
        "directions": [{"id": "direction_1", "title": "举杯的一刻", "summary": "以已核对的举杯片段组织短片。",
                        "evidence": [dict(EVIDENCE[0])]}],
        "uncertainties": ["人物身份尚未确认。"],
    }


def envelope(content=None, usage=None):
    return {"id": "request-test-1", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(payload(), ensure_ascii=False) if content is None else content}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100} if usage is None else usage}


def make_director(handler, **kwargs):
    return CloudDirector(dict(CONFIG), client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), budget_check=lambda reservation: reservation == "reserved", **kwargs)


def propose(director, evidence=None, **kwargs):
    return asyncio.run(director.propose(EVIDENCE if evidence is None else evidence, ASSETS,
                                       reservation="reserved", **kwargs))


def test_parse_json_and_one_outer_fence():
    content = json.dumps(payload(), ensure_ascii=False)
    assert parse_result(content, ASSETS).directions[0].evidence[0].start == 2
    assert parse_result("```json\n" + content + "\n```", ASSETS).directions[0].id == "direction_1"


@pytest.mark.parametrize("content", [
    "Some explanation\n{}", "```json\n{}\n```\nMore text", "[]", "null",
    '{"directions":[],"uncertainties":[]}',
    '{"directions":[],"uncertainties":["不足"],"command":"run"}',
    '{"directions":[],"directions":[],"uncertainties":["不足"]}',
    '{"directions":[],"uncertainties":[true]}',
    '{"directions":[],"uncertainties":[" "]}',
])
def test_reject_invalid_or_executable_structure(content):
    with pytest.raises(DirectorError) as error:
        parse_result(content, ASSETS)
    assert error.value.code == "INVALID_MODEL_OUTPUT"


def test_abstain_requires_a_reason():
    result = parse_result('{"directions":[],"uncertainties":["没有足够的事件证据"]}', ASSETS)
    assert result.directions == []


@pytest.mark.parametrize("change", [
    {"asset_id": "missing"}, {"start": -1}, {"start": 8}, {"end": 21},
    {"start": "2"}, {"start": True}, {"end": float("nan")}, {"tool": "shell"},
])
def test_invalid_spans(change):
    data = payload()
    data["directions"][0]["evidence"][0].update(change)
    with pytest.raises(DirectorError):
        parse_result(json.dumps(data), ASSETS)


def test_unique_direction_ids_and_count():
    data = payload()
    data["directions"] *= 2
    with pytest.raises(DirectorError):
        parse_result(json.dumps(data), ASSETS)
    data["directions"] = [{**data["directions"][0], "id": f"d{i}"} for i in range(4)]
    with pytest.raises(DirectorError):
        parse_result(json.dumps(data), ASSETS)


@pytest.mark.parametrize("config", [
    {}, {"MODEL_API_KEY": "test-key"},
    {**CONFIG, "CLOUD_ENABLED": "true"}, {**CONFIG, "CLOUD_RETENTION_VERIFIED": False},
    {**CONFIG, "CLOUD_REGION_VERIFIED": 1}, {**CONFIG, "MODEL_NAME": "another-model"},
    {**CONFIG, "MODEL_API_KEY": ""},
])
def test_guards_before_client_creation(config):
    def must_not_create():
        pytest.fail("client must not be created before admission")
    director = CloudDirector(config, client_factory=must_not_create, budget_check=lambda _: True)
    assert director.ready()[0] is False
    with pytest.raises(DirectorError):
        propose(director)


@pytest.mark.parametrize("url", [
    "http://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1",
    "https://127.0.0.1/compatible-mode/v1",
    "https://dashscope.aliyuncs.com:444/compatible-mode/v1",
    "https://user@dashscope.aliyuncs.com/compatible-mode/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1?url=http://localhost",
    "https://dashscope.aliyuncs.com/compatible-mode/v1#fragment",
    "https://dashscope.aliyuncs.com/other-path",
])
def test_ssrf_endpoint_denied_before_client_creation(url):
    director = CloudDirector({**CONFIG, "MODEL_BASE_URL": url},
                             client_factory=lambda: pytest.fail("unsafe endpoint created client"),
                             budget_check=lambda _: True)
    with pytest.raises(DirectorError) as error:
        propose(director)
    assert error.value.code == "ENDPOINT_NOT_ALLOWED"


@pytest.mark.parametrize("permission", [None, lambda _: False, lambda _: 1])
def test_missing_budget_permission_blocks_before_client(permission):
    director = CloudDirector(CONFIG, client_factory=lambda: pytest.fail("no budget permission"), budget_check=permission)
    with pytest.raises(DirectorError) as error:
        propose(director)
    assert error.value.code == "BUDGET_PERMISSION_REQUIRED"
    assert error.value.usage["cost_status"] == "not_sent"


def test_no_evidence_never_invents_from_asset_metadata():
    director = CloudDirector(CONFIG, client_factory=lambda: pytest.fail("missing evidence"), budget_check=lambda _: True)
    with pytest.raises(DirectorError) as error:
        propose(director, evidence=[])
    assert error.value.code == "EVIDENCE_REQUIRED"


def test_initialisation_failure_is_sanitised_and_not_sent():
    def fail():
        raise RuntimeError("test-key: internal secret")
    director = CloudDirector(CONFIG, client_factory=fail, budget_check=lambda _: True)
    with pytest.raises(DirectorError) as error:
        propose(director)
    assert error.value.code == "CLIENT_INIT_FAILED"
    assert "test-key" not in str(error.value)
    assert error.value.usage["cost_status"] == "not_sent"


def test_success_only_sends_text_evidence_and_has_actual_usage():
    def respond(request):
        assert str(request.url) == CONFIG["MODEL_BASE_URL"] + "/chat/completions"
        body = json.loads(request.content)
        assert body["max_tokens"] == 4096
        assert "没有读取原视频" in body["messages"][0]["content"]
        user = json.loads(body["messages"][1]["content"])
        assert user["verified_evidence"] == EVIDENCE
        assert "assets" not in user
        return httpx.Response(200, json=envelope())
    result = propose(make_director(respond))
    assert result["result"].directions[0].id == "direction_1"
    assert result["usage"] == {"input_tokens": 1000, "output_tokens": 100, "cost_yuan": 0.002,
                               "cost_status": "known", "request_id": "request-test-1",
                               "model": "qwen3-vl-plus", "retries": 0}


def test_timeout_keeps_cost_unknown_without_retry():
    requests = []
    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("test-key private response", request=request)
    with pytest.raises(DirectorError) as error:
        propose(make_director(timeout))
    assert error.value.code == "MODEL_TIMEOUT"
    assert error.value.usage["cost_yuan"] is None
    assert len(requests) == 1
    assert "test-key" not in str(error.value)


def test_connection_retry_is_bounded_and_unknown_cost_is_preserved():
    requests = []
    def fail(request):
        requests.append(request)
        raise httpx.ConnectError("connection failed", request=request)
    with pytest.raises(DirectorError) as error:
        propose(make_director(fail))
    assert len(requests) == 2
    assert error.value.usage["retries"] == 1
    assert error.value.usage["cost_yuan"] is None


def test_success_after_retry_does_not_claim_first_attempt_was_free():
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectTimeout("connection timeout", request=request)
        return httpx.Response(200, json=envelope())
    result = propose(make_director(respond))
    assert result["usage"]["cost_yuan"] is None
    assert result["usage"]["input_tokens"] == 1000
    assert result["usage"]["retries"] == 1


def test_retry_rechecks_persisted_budget_permission():
    checked = []
    sent = []
    def permission(reservation):
        checked.append(reservation)
        return len(checked) == 1
    def fail(request):
        sent.append(request)
        raise httpx.ConnectError("connection failed", request=request)
    director = CloudDirector(CONFIG, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fail)),
                             budget_check=permission)
    with pytest.raises(DirectorError) as error:
        propose(director)
    assert error.value.code == "BUDGET_PERMISSION_REQUIRED"
    assert error.value.usage["cost_yuan"] is None
    assert len(sent) == 1 and len(checked) == 2


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_error_or_redirect_never_follows_or_fakes_free_call(status):
    sent = []
    def respond(request):
        sent.append(request)
        return httpx.Response(status, headers={"Location": "https://other.test/private"}, text="private provider text")
    with pytest.raises(DirectorError) as error:
        propose(make_director(respond))
    assert len(sent) == 1
    assert error.value.code == "MODEL_HTTP_ERROR"
    assert error.value.usage["cost_yuan"] is None
    assert "private" not in str(error.value)


@pytest.mark.parametrize("bad_usage", [{}, {"prompt_tokens": True, "completion_tokens": 1},
                                       {"prompt_tokens": -1, "completion_tokens": 1}])
def test_missing_or_invalid_usage_does_not_become_zero(bad_usage):
    result = propose(make_director(lambda request: httpx.Response(200, json=envelope(usage=bad_usage))))
    assert result["usage"]["cost_yuan"] is None
    assert result["usage"]["cost_status"] == "unknown"


def test_schema_failure_still_returns_billed_usage():
    with pytest.raises(DirectorError) as error:
        propose(make_director(lambda request: httpx.Response(200, json=envelope(content='{"command":"run"}'))))
    assert error.value.code == "INVALID_MODEL_OUTPUT"
    assert error.value.usage["cost_yuan"] == 0.002


def test_output_cannot_cite_unconfirmed_interval_even_inside_asset():
    data = payload()
    data["directions"][0]["evidence"][0]["end"] = 9.0
    with pytest.raises(DirectorError) as error:
        propose(make_director(lambda request: httpx.Response(200, json=envelope(content=json.dumps(data)))))
    assert error.value.code == "UNGROUNDED_EVIDENCE"
    assert error.value.usage["cost_yuan"] == 0.002


def test_worst_case_reservation_covers_two_full_requests():
    assert CloudDirector.required_reservation_yuan >= 2 * (262144 * 3 + 4096 * 30) / 1_000_000
