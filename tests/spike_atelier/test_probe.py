import base64
import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from app.spike_atelier.probe import HEADERS, KEYPATHS, ProbeConfig, fingerprint, handle
from scripts.spike_mcp_probe import (
    prepare,
    private_read,
    private_write,
    provider_projection,
    run,
    safe_capture,
    summarize,
)


@pytest.fixture
def config():
    now = int(time.time())
    return ProbeConfig(
        str(uuid4()),
        now - 1,
        now + 120,
        "probe-bearer-" + "s" * 32,
        "probe-signing-" + "k" * 32,
        {"E1": "n" * 32, "E2": "z" * 32},
        hold_ms=0,
    )


def request(config, limit=7, tag="E1", method="tools/call"):
    return {
        "httpMethod": "POST",
        "headers": {
            "Authorization": "Bearer " + config.bearer,
            "X-Spike-Event-Binding": config.binding(tag),
        },
        "body": json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "caller-rpc-id",
                "method": method,
                "params": {"name": "list_tasks", "arguments": {"limit": limit}},
            }
        ),
    }


def call(config, event, **kwargs):
    captured = []
    result = handle(event, config, emit=captured.append, **kwargs)
    assert len(captured) == 1
    return result, json.loads(captured[0])


@pytest.mark.parametrize("limit,tag,expected_q", [(7, "E1", "Q1"), (11, "E2", "Q2")])
def test_matching_capture_keeps_unknown_origin_not_verified(config, limit, tag, expected_q):
    result, observation = call(config, request(config, limit, tag))
    assert result["statusCode"] == 200
    content = json.loads(result["body"])["result"]["content"]
    assert content == [{"type": "text", "text": '{"items":[]}'}]
    assert observation["request_tag"] == expected_q
    assert observation["argument_fingerprint"] == fingerprint({"limit": limit})
    assert observation["binding_tag"] == tag
    assert observation["verdict"] == "NOT_VERIFIED"
    assert set(observation["headers"]) == set(HEADERS)
    assert set(observation["keypaths"]) == set(KEYPATHS)
    assert safe_capture(observation, config) == observation


@pytest.mark.parametrize("limit,tag", [(7, "E2"), (11, "E1")])
def test_swapped_binding_and_independent_fingerprint_fail_403(config, limit, tag):
    result, observation = call(config, request(config, limit, tag))
    assert result["statusCode"] == 403
    assert json.loads(result["body"]) == {"error": "correlation_failed"}
    assert observation["argument_fingerprint"] == fingerprint({"limit": limit})
    assert observation["binding_tag"] == tag and observation["verdict"] == "FAIL"


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing_auth", 401),
        ("wrong_auth", 401),
        ("missing_binding", 403),
        ("wrong_binding", 403),
        ("expired_binding", 403),
        ("expired_probe", 410),
        ("origin", 403),
        ("duplicate_auth", 401),
    ],
)
def test_authority_and_expiry_fail_closed(config, case, status):
    event = request(config)
    if case == "missing_auth":
        event["headers"].pop("Authorization")
    elif case == "wrong_auth":
        event["headers"]["Authorization"] = "wrong-credential"
    elif case == "missing_binding":
        event["headers"].pop("X-Spike-Event-Binding")
    elif case == "wrong_binding":
        event["headers"]["X-Spike-Event-Binding"] += "tamper"
    elif case == "expired_binding":
        expired = replace(config, issued_at=config.issued_at - 10, expires_at=config.issued_at)
        event["headers"]["X-Spike-Event-Binding"] = expired.binding("E1")
    elif case == "expired_probe":
        config = replace(config, issued_at=config.issued_at - 10, expires_at=config.issued_at)
    elif case == "origin":
        event["headers"]["Origin"] = "https://untrusted.example"
    else:
        event["headers"]["authorization"] = "duplicate"
    result, observation = call(config, event)
    assert result["statusCode"] == status
    assert observation["verdict"] == "REJECTED"
    assert "wrong-credential" not in json.dumps(observation)


@pytest.mark.parametrize(
    "arguments",
    [{"limit": True}, {"limit": 8}, {}, {"limit": 7, "owner_id": 42}, {"limit": "7"}, []],
)
def test_strict_synthetic_schema_no_authority_fields(config, arguments):
    event = request(config)
    payload = json.loads(event["body"])
    payload["params"]["arguments"] = arguments
    event["body"] = json.dumps(payload)
    result, observation = call(config, event)
    assert json.loads(result["body"])["error"]["code"] == -32602
    assert observation["argument_fingerprint"] is None
    assert observation["binding_tag"] is None


def test_discovery_notification_protocol_without_dataset_binding(config):
    for method in ("initialize", "tools/list", "notifications/initialized"):
        event = request(config, method=method)
        event["headers"].pop("X-Spike-Event-Binding")
        body = {"jsonrpc": "2.0", "method": method}
        if method != "notifications/initialized":
            body["id"] = 1
        event["body"] = json.dumps(body)
        result, observation = call(config, event)
        assert result["statusCode"] == (202 if "notifications" in method else 200)
        assert observation["verdict"] == "NOT_VERIFIED" and observation["request_tag"] is None
        if method == "tools/list":
            tools = json.loads(result["body"])["result"]["tools"]
            assert len(tools) == 1 and tools[0]["name"] == "list_tasks"
            assert tools[0]["inputSchema"]["properties"]["limit"]["enum"] == [7, 11]


@pytest.mark.parametrize(
    "location",
    ["method", "tool", "header_name", "header_value", "rpc_id", "arguments", "extra_key"],
)
def test_planted_secrets_never_enter_capture_or_provider_projection(config, location):
    secret = "PLANTED-TOKEN-model-user-text"
    event = request(config)
    payload = json.loads(event["body"])
    if location == "method":
        payload["method"] = secret
    elif location == "tool":
        payload["params"]["name"] = secret
    elif location == "header_name":
        event["headers"][secret] = secret
    elif location == "header_value":
        event["headers"]["Traceparent"] = secret
    elif location == "rpc_id":
        payload["id"] = secret
    elif location == "arguments":
        payload["params"]["arguments"]["extra"] = secret
    else:
        payload[secret] = secret
    event["body"] = json.dumps(payload)
    _, observation = call(config, event)
    serialized = json.dumps(observation)
    assert secret not in serialized
    assert config.bearer not in serialized and config.signing_key not in serialized
    assert config.binding("E1") not in serialized
    projected = provider_projection(
        {
            "id": secret,
            "status": secret,
            "tools": [{"headers": {"Authorization": secret}}],
            "output": [
                {"type": "message", "text": secret},
                {"type": secret, "text": secret},
                {"type": "mcp_call", "name": secret, "arguments": secret},
            ],
            "usage": {"input_tokens": secret, "output_tokens": 4, secret: secret},
        }
    )
    assert secret not in json.dumps(projected)


@pytest.mark.parametrize(
    "raw,status",
    [
        ("not-json", 400),
        ("x" * 17000, 413),
        ("[" * 1500 + "]" * 1500, 400),
        (json.dumps({"method": []}), 200),
    ],
)
def test_malformed_bounded_input(config, raw, status):
    event = request(config)
    event["body"] = raw
    result, _ = call(config, event)
    assert result["statusCode"] == status


def test_base64_proxy_and_expiration_during_processing(config):
    event = request(config)
    event["isBase64Encoded"] = True
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    assert call(config, event)[0]["statusCode"] == 200
    values = iter([config.expires_at - 1, config.expires_at - 1, config.expires_at])
    assert call(config, event, clock=lambda: next(values))[0]["statusCode"] == 410


def test_private_package_contains_only_reviewed_code_and_fresh_probe_config(tmp_path):
    import zipfile

    directory = tmp_path / "run"
    safe = prepare(directory, 120)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with zipfile.ZipFile(directory / "function.zip") as archive:
        assert set(archive.namelist()) == {"probe.py", "_probe_private.json"}
        config = ProbeConfig(**json.loads(archive.read("_probe_private.json")))
    assert config.run_uuid == safe["run_uuid"]
    assert config.bearer not in json.dumps(safe)
    assert config.signing_key not in json.dumps(safe)
    with pytest.raises(ValueError):
        prepare(tmp_path / "too-long", 1801)


def test_parallel_requests_have_distinct_bindings_and_no_global_owner(config):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda pair: call(config, request(config, *pair)), [(7, "E1"), (11, "E2")])
        )
    assert {o["request_tag"] for _, o in results} == {"Q1", "Q2"}
    assert len({o["binding_hash"] for _, o in results}) == 2


def test_runner_fixed_projections_and_fail_closed_origin_correlation(tmp_path):
    directory = tmp_path / "run"
    prepare(directory, 120)
    config = ProbeConfig(**json.loads(private_read(directory / "private.json")))
    key_file = tmp_path / "provider.key"
    private_write(key_file, "private-provider-key-" + "a" * 32)
    captures = []

    def send(url, headers, payload, timeout=15):
        if url.endswith("/mcp"):
            result = handle(
                {"httpMethod": "POST", "headers": headers, "body": json.dumps(payload)},
                config,
                emit=lambda value: captures.append(json.loads(value)),
            )
            return result["statusCode"], json.loads(result["body"]), {}, None
        tool = payload["tools"][0]
        limit = 7 if "limit=7" in payload["input"][0]["content"] else 11
        result = handle(
            request_for_provider(tool["headers"], limit),
            config,
            emit=lambda value: captures.append(json.loads(value)),
        )
        assert result["statusCode"] == 200
        return (
            200,
            {
                "id": str(uuid4()),
                "status": "completed",
                "tools": [{"headers": {"Authorization": "planted-response-secret"}}],
                "output": [
                    {
                        "type": "mcp_call",
                        "name": "list_tasks",
                        "arguments": json.dumps({"limit": limit}),
                        "error": None,
                    }
                ],
                "usage": {"input_tokens": 10},
            },
            {},
            None,
        )

    evidence = run(
        directory,
        "https://fixture.apigw.yandexcloud.net/mcp",
        "gpt://test-folder/yandexgpt",
        key_file,
        send=send,
    )
    serialized = json.dumps(evidence)
    for secret in (
        "planted-response-secret",
        config.bearer,
        config.signing_key,
        config.binding("E1"),
        private_read(key_file),
    ):
        assert secret not in serialized
    assert evidence["transport_correlation"] == "NOT_VERIFIED"
    result = summarize(config, evidence, captures)
    assert result["transport_correlation"] == "PASS" and result["call_overlap_ms"] > 0
    assert result["telegram_acceptance"] == "NOT_RUN"
    unknown = {**evidence, "requests": [{**r, "outputs": []} for r in evidence["requests"]]}
    assert summarize(config, unknown, captures)["transport_correlation"] == "NOT_VERIFIED"
    # A planted arbitrary key invalidates capture rather than being exported.
    planted = {**captures[-1], "secret-header-value": "planted-response-secret"}
    assert safe_capture(planted, config) is None
    assert "planted-response-secret" not in json.dumps(summarize(config, evidence, [planted]))


def request_for_provider(headers, limit):
    return {
        "httpMethod": "POST",
        "headers": headers,
        "body": json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_tasks", "arguments": {"limit": limit}},
            }
        ),
    }


def test_projection_unknown_value_types_and_missing_overlap_remain_unverified(config):
    assert (
        provider_projection({"status": {"secret": "planted"}, "output": []})["status"] == "unknown"
    )
    _, observation = call(config, request(config))
    malformed = {**observation, "method": ["planted"]}
    assert safe_capture(malformed, config) is None
    assert (
        summarize(config, {"run_uuid": config.run_uuid, "requests": []}, [observation])[
            "transport_correlation"
        ]
        == "NOT_VERIFIED"
    )


def test_private_file_permissions_and_bad_negative_prevent_provider_requests(tmp_path):
    directory = tmp_path / "run"
    prepare(directory, 120)
    key_file = tmp_path / "key"
    private_write(key_file, "private-provider-key-" + "a" * 32)
    calls = []

    def send(url, headers, payload, timeout=15):
        calls.append(url)
        return 403, {"error": "unexpected-secret"}, {}, None

    evidence = run(
        directory,
        "https://fixture.apigw.yandexcloud.net/mcp",
        "gpt://test-folder/yandexgpt",
        key_file,
        send=send,
    )
    assert len(calls) == 4 and all(url.endswith("/mcp") for url in calls)
    assert evidence["requests"] == []
    assert "unexpected-secret" not in json.dumps(evidence)
    key_file.chmod(0o644)
    with pytest.raises(ValueError, match="private_regular_file_required"):
        private_read(key_file)
