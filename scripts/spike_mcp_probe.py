"""Private capture probe preparation, bounded provider run and redacted evidence."""

import argparse
import json
import os
import re
import secrets
import stat
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.spike_atelier.probe import HEADERS, KEYPATHS, TYPES, VERDICTS, ProbeConfig, fingerprint

PROVIDER_URL = "https://ai.api.cloud.yandex.net/v1/responses"
OUTPUT_TYPES = {"mcp_call", "mcp_list_tools", "message", "mcp_approval_request"}


def private_write(path, value):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w") as stream:
        stream.write(value)


def private_read(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags)) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
            raise ValueError("private_regular_file_required")
        return stream.read()


def prepare(directory, ttl=1800):
    if type(ttl) is not int or not 1 <= ttl <= 1800:
        raise ValueError("invalid_ttl")
    directory.mkdir(mode=0o700)
    issued = int(time.time())
    config = ProbeConfig(
        run_uuid=str(uuid4()),
        issued_at=issued,
        expires_at=issued + ttl,
        bearer=secrets.token_urlsafe(32),
        signing_key=secrets.token_urlsafe(32),
        nonces={tag: secrets.token_hex(16) for tag in ("E1", "E2")},
    )
    raw = json.dumps(vars(config), separators=(",", ":"))
    private_write(directory / "private.json", raw)
    expected = {
        q: {"fingerprint": fingerprint({"limit": limit}), "event": event}
        for q, limit, event in (("Q1", 7, "E1"), ("Q2", 11, "E2"))
    }
    private_write(directory / "expected.json", json.dumps(expected))
    source = Path(__file__).parents[1] / "app/spike_atelier/probe.py"
    archive = directory / "function.zip"
    descriptor = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream, zipfile.ZipFile(stream, "w") as package:
        package.writestr("probe.py", source.read_bytes())
        package.writestr("_probe_private.json", raw)
    return {"run_uuid": config.run_uuid, "expires_at": config.expires_at}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(url, headers, payload, timeout=15):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as reply:
            raw = reply.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return reply.status, None, {}, "INVALID_RESPONSE"
            return reply.status, json.loads(raw), dict(reply.headers), None
    except urllib.error.HTTPError as exc:
        # Parse privately for fixed negative verdicts; never log the body or exception.
        try:
            raw = exc.read(65537)
            body = json.loads(raw) if len(raw) <= 65536 else None
        except (ValueError, RecursionError):
            body = None
        return exc.code, body, {}, "HTTP_ERROR"
    except (TimeoutError, urllib.error.URLError):
        return None, None, {}, "NETWORK_ERROR"
    except (ValueError, RecursionError):
        return None, None, {}, "INVALID_RESPONSE"


def endpoint(url):
    from urllib.parse import urlsplit

    value = urlsplit(url)
    if (
        value.scheme != "https"
        or not value.hostname
        or not value.hostname.endswith(".apigw.yandexcloud.net")
        or value.username
        or value.password
        or value.query
        or value.fragment
        or value.port not in (None, 443)
        or value.path != "/mcp"
    ):
        raise ValueError("temporary_https_gateway_required")
    return url


def provider_payload(config, url, model, request_tag):
    limit, event = (7, "E1") if request_tag == "Q1" else (11, "E2")
    if request_tag not in {"Q1", "Q2"}:
        raise ValueError("invalid_request_tag")
    return {
        "model": model,
        "store": False,
        "max_output_tokens": 256,
        "max_tool_calls": 2,
        "parallel_tool_calls": False,
        "input": [
            {
                "role": "user",
                "content": f"Call list_tasks exactly once with limit={limit}. Synthetic fixture.",
            }
        ],
        "tools": [
            {
                "type": "mcp",
                "server_label": "capture_fixture",
                "server_url": url,
                "allowed_tools": ["list_tasks"],
                "require_approval": "never",
                "headers": {
                    "Authorization": "Bearer " + config.bearer,
                    "X-Spike-Event-Binding": config.binding(event),
                },
            }
        ],
    }


def safe_identifier(value):
    if not isinstance(value, str):
        return None
    prefix = "resp_" if value.startswith("resp_") else ""
    try:
        return prefix + str(UUID(value.removeprefix(prefix)))
    except ValueError:
        return None


def provider_projection(body):
    body = body if isinstance(body, dict) else {}
    safe = {
        "response_id": safe_identifier(body.get("id")),
        "status": body.get("status")
        if isinstance(body.get("status"), str)
        and body.get("status")
        in {
            "completed",
            "incomplete",
            "failed",
            "in_progress",
            "queued",
        }
        else "unknown",
        "usage": {},
        "outputs": [],
    }
    usage = body.get("usage", {})
    if isinstance(usage, dict):
        safe["usage"] = {
            key: usage[key]
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
            )
            if type(usage.get(key)) is int and 0 <= usage[key] <= 1000000
        }
    output = body.get("output", [])
    if not isinstance(output, list) or len(output) > 16:
        return safe
    for item in output:
        item = item if isinstance(item, dict) else {}
        kind = item.get("type")
        entry = {"type": kind if isinstance(kind, str) and kind in OUTPUT_TYPES else "unsupported"}
        if kind == "mcp_call":
            entry.update(
                tool="list_tasks" if item.get("name") == "list_tasks" else "unsupported",
                fingerprint=None,
                is_error=bool(item.get("error")),
            )
            try:
                args = item.get("arguments")
                args = json.loads(args) if isinstance(args, str) else args
                if entry["tool"] == "list_tasks":
                    entry["fingerprint"] = fingerprint(args)
            except (ValueError, RecursionError):
                pass
        safe["outputs"].append(entry)
    return safe


def negative_probes(config, url, send=post):
    valid = {"Authorization": "Bearer " + config.bearer}
    requests = [
        ("auth", {}, {}, 401),
        ("missing_binding", valid, {"limit": 7}, 403),
        ("swap_Q1", {**valid, "X-Spike-Event-Binding": config.binding("E2")}, {"limit": 7}, 403),
        ("swap_Q2", {**valid, "X-Spike-Event-Binding": config.binding("E1")}, {"limit": 11}, 403),
    ]
    results = []
    expected_error = {
        "auth": "unauthorized",
        "missing_binding": "binding_rejected",
        "swap_Q1": "correlation_failed",
        "swap_Q2": "correlation_failed",
    }
    for label, headers, arguments, expected in requests:
        status, body, _, error = send(
            url,
            headers,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_tasks", "arguments": arguments},
            },
        )
        results.append(
            {
                "check": label,
                "status_code": status,
                "expected": expected,
                "verdict": "PASS"
                if (
                    status == expected
                    and isinstance(body, dict)
                    and body.get("error") == expected_error[label]
                )
                else "FAIL",
                "error": error,
            }
        )
    return results


def run(directory, url, model, key_file, send=post):
    url = endpoint(url)
    if not re.fullmatch(r"gpt://[a-zA-Z0-9_-]+/[a-zA-Z0-9_/.-]+", model):
        raise ValueError("explicit_model_uri_required")
    config = ProbeConfig(**json.loads(private_read(directory / "private.json")))
    expected = json.loads(private_read(directory / "expected.json"))
    if expected != {
        q: {"fingerprint": fingerprint({"limit": limit}), "event": event}
        for q, limit, event in (("Q1", 7, "E1"), ("Q2", 11, "E2"))
    }:
        raise ValueError("expected_map_mismatch")
    if time.time() >= config.expires_at:
        raise ValueError("probe_expired")
    key = private_read(key_file).strip()
    if not 16 <= len(key) <= 512 or not key.isascii() or any(char.isspace() for char in key):
        raise ValueError("private_provider_key_required")
    negatives = negative_probes(config, url, send)
    evidence = {
        "run_uuid": config.run_uuid,
        "negatives": negatives,
        "requests": [],
        "transport_correlation": "NOT_VERIFIED",
        "telegram_acceptance": "NOT_RUN",
        "native_identity": "NOT_VERIFIED",
        "cost": "UNKNOWN",
    }
    if all(item["verdict"] == "PASS" for item in negatives):
        barrier = threading.Barrier(2)

        def invoke(request_tag):
            payload = provider_payload(config, url, model, request_tag)
            barrier.wait(timeout=5)
            started = time.time()
            status, body, headers, error = send(
                PROVIDER_URL,
                {"Authorization": "Api-Key " + key},
                payload,
                timeout=15,
            )
            ended = time.time()
            result = provider_projection(body)
            request_id = next((v for k, v in headers.items() if k.lower() == "x-request-id"), None)
            return {
                "request_tag": request_tag,
                "started_at": started,
                "finished_at": ended,
                "latency_ms": max(0, int((ended - started) * 1000)),
                "status_code": status,
                "request_id": safe_identifier(request_id),
                "error": error,
                **result,
            }

        with ThreadPoolExecutor(max_workers=2) as executor:
            evidence["requests"] = list(executor.map(invoke, ("Q1", "Q2")))
    private_write(directory / "provider-evidence.json", json.dumps(evidence, indent=2))
    return evidence


def safe_capture(value, config):
    expected_fields = {
        "run_uuid",
        "timestamp",
        "latency_ms",
        "method",
        "tool",
        "argument_fingerprint",
        "argument_tag",
        "request_tag",
        "binding_hash",
        "binding_tag",
        "verdict",
        "keypaths",
        "headers",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value["run_uuid"] != config.run_uuid
    ):
        return None
    if not isinstance(value["method"], str) or value["method"] not in {
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "unsupported",
    }:
        return None
    if (
        not isinstance(value["tool"], str)
        or value["tool"] not in {"list_tasks", "unsupported"}
        or not isinstance(value["verdict"], str)
        or value["verdict"] not in VERDICTS
    ):
        return None
    if type(value["latency_ms"]) is not int or not 0 <= value["latency_ms"] <= 5000:
        return None
    try:
        timestamp = value["timestamp"]
        if not isinstance(timestamp, str) or len(timestamp) > 40:
            return None
        instant = datetime.fromisoformat(timestamp)
        if (
            instant.tzinfo is None
            or not config.issued_at <= instant.timestamp() <= config.expires_at + 5
        ):
            return None
        if value["argument_tag"] is not None:
            limit = {"F1": 7, "F2": 11}[value["argument_tag"]]
            digest, (_, q, _) = config.expected({"limit": limit})
            if value["argument_fingerprint"] != digest or value["request_tag"] != q:
                return None
        elif value["argument_fingerprint"] is not None or value["request_tag"] is not None:
            return None
        tag = value["binding_tag"]
        if tag is not None:
            _, digest = config.verify(config.binding(tag), config.issued_at)
            if value["binding_hash"] != digest:
                return None
        elif value["binding_hash"] is not None:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    for key, allowed in (("headers", HEADERS), ("keypaths", KEYPATHS)):
        mapping = value[key]
        if not isinstance(mapping, dict) or set(mapping) != set(allowed):
            return None
        for item in mapping.values():
            if (
                not isinstance(item, dict)
                or set(item) != {"present", "type"}
                or type(item["present"]) is not bool
                or not isinstance(item["type"], str)
                or item["type"] not in TYPES
            ):
                return None
    return value


def summarize(config, provider, observations):
    captures = [safe for value in observations if (safe := safe_capture(value, config))]
    result = {
        "run_uuid": config.run_uuid,
        "captures": captures,
        "transport_correlation": "NOT_VERIFIED",
        "call_overlap_ms": None,
        "telegram_acceptance": "NOT_RUN",
        "native_identity": "NOT_VERIFIED",
        "cost": "UNKNOWN",
    }
    positive = [
        v for v in captures if v["method"] == "tools/call" and v["verdict"] == "NOT_VERIFIED"
    ]
    if not isinstance(provider, dict) or provider.get("run_uuid") != config.run_uuid:
        return result
    negatives, requests = provider.get("negatives", []), provider.get("requests", [])
    if (
        len(negatives) != 4
        or any(n.get("verdict") != "PASS" for n in negatives)
        or len(requests) != 2
    ):
        return result
    if {r.get("request_tag") for r in requests} != {"Q1", "Q2"} or len(positive) != 2:
        return result
    expected_negatives = {"auth": 401, "missing_binding": 403, "swap_Q1": 403, "swap_Q2": 403}
    if {n.get("check") for n in negatives} != set(expected_negatives) or any(
        n.get("status_code") != expected_negatives[n["check"]] for n in negatives
    ):
        return result
    swaps = [v for v in captures if v["verdict"] == "FAIL"]
    if len(swaps) != 2 or {(v["request_tag"], v["binding_tag"]) for v in swaps} != {
        ("Q1", "E2"),
        ("Q2", "E1"),
    }:
        return result
    intervals = []
    for record in requests:
        q = record["request_tag"]
        limit, expected_event = (7, "E1") if q == "Q1" else (11, "E2")
        digest = fingerprint({"limit": limit})
        calls = [o for o in record.get("outputs", []) if o.get("type") == "mcp_call"]
        matches = [v for v in positive if v["request_tag"] == q]
        if (
            record.get("status_code") != 200
            or record.get("status") != "completed"
            or len(calls) != 1
            or calls[0].get("tool") != "list_tasks"
            or calls[0].get("fingerprint") != digest
            or calls[0].get("is_error") is not False
            or len(matches) != 1
            or matches[0]["binding_tag"] != expected_event
            or matches[0]["argument_fingerprint"] != digest
        ):
            return result
        capture = matches[0]
        start = datetime.fromisoformat(capture["timestamp"]).timestamp()
        end = start + capture["latency_ms"] / 1000
        if not record["started_at"] <= start <= end <= record["finished_at"]:
            return result
        intervals.append((start, end))
    overlap = min(i[1] for i in intervals) - max(i[0] for i in intervals)
    result["call_overlap_ms"] = max(0, int(overlap * 1000))
    if overlap > 0:
        result["transport_correlation"] = "PASS"
    return result


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--ttl", type=int, default=1800)
    p = commands.add_parser("run")
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--provider-key-file", type=Path, required=True)
    p = commands.add_parser("summarize")
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--captures-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.directory, args.ttl)
        elif args.command == "run":
            result = run(args.directory, args.url, args.model, args.provider_key_file)
        else:
            config = ProbeConfig(**json.loads(private_read(args.directory / "private.json")))
            provider = json.loads(private_read(args.directory / "provider-evidence.json"))
            # Input is the private yc logging JSON response; never copy it to output.
            raw = json.loads(private_read(args.captures_file))
            rows = raw.get("entries", []) if isinstance(raw, dict) else raw
            observations = []
            for row in rows if isinstance(rows, list) else []:
                value = row.get("message") if isinstance(row, dict) else None
                try:
                    observations.append(json.loads(value) if isinstance(value, str) else row)
                except ValueError:
                    pass
            result = summarize(config, provider, observations)
            private_write(args.directory / "capture-evidence.json", json.dumps(result, indent=2))
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(json.dumps({"result": "FAILED", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
