"""Capture-only MCP fixture. Standalone stdlib module for a temporary Cloud Function."""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

MAX_BODY = 16 * 1024
METHODS = {"initialize", "notifications/initialized", "tools/list", "tools/call"}
HEADERS = (
    "Authorization",
    "X-Spike-Event-Binding",
    "Content-Type",
    "MCP-Protocol-Version",
    "MCP-Session-Id",
    "X-Request-Id",
    "Traceparent",
)
KEYPATHS = (
    "jsonrpc",
    "id",
    "method",
    "params",
    "params.name",
    "params.arguments",
    "params.arguments.limit",
)
TYPES = {"missing", "null", "boolean", "number", "string", "array", "object"}
VERDICTS = {"REJECTED", "NOT_VERIFIED", "FAIL"}


def json_type(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def fingerprint(arguments):
    if (
        not isinstance(arguments, dict)
        or set(arguments) != {"limit"}
        or type(arguments["limit"]) is not int
        or arguments["limit"] not in {7, 11}
    ):
        raise ValueError("invalid_probe_arguments")
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def projection(body, headers):
    paths = {}
    for path in KEYPATHS:
        value = body
        present = True
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                present = False
                break
            value = value[key]
        paths[path] = {"present": present, "type": json_type(value) if present else "missing"}
    header_types = {}
    for name in HEADERS:
        values = [v for k, v in headers.items() if isinstance(k, str) and k.lower() == name.lower()]
        header_types[name] = {
            "present": bool(values),
            "type": json_type(values[0])
            if len(values) == 1
            else ("array" if values else "missing"),
        }
    return paths, header_types


def header(headers, name):
    values = [v for k, v in headers.items() if isinstance(k, str) and k.lower() == name.lower()]
    return (
        values[0]
        if (len(values) == 1 and isinstance(values[0], str) and len(values[0]) <= 512)
        else ""
    )


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@dataclass(frozen=True, repr=False)
class ProbeConfig:
    run_uuid: str
    issued_at: int
    expires_at: int
    bearer: str
    signing_key: str
    nonces: dict
    hold_ms: int = 400

    def __post_init__(self):
        if str(UUID(self.run_uuid)) != self.run_uuid:
            raise ValueError("invalid_run")
        if (
            type(self.issued_at) is not int
            or type(self.expires_at) is not int
            or not 1 <= self.expires_at - self.issued_at <= 1800
            or type(self.hold_ms) is not int
            or not 0 <= self.hold_ms <= 500
            or not isinstance(self.bearer, str)
            or not 32 <= len(self.bearer) <= 128
            or not isinstance(self.signing_key, str)
            or not 32 <= len(self.signing_key) <= 128
            or self.bearer == self.signing_key
            or not isinstance(self.nonces, dict)
            or set(self.nonces) != {"E1", "E2"}
            or any(not isinstance(n, str) or not 16 <= len(n) <= 64 for n in self.nonces.values())
            or self.nonces["E1"] == self.nonces["E2"]
        ):
            raise ValueError("invalid_probe_config")

    def expected(self, arguments):
        # Correlation comes from the independently fixed request fixture, never the header.
        digest = fingerprint(arguments)
        mapping = {
            fingerprint({"limit": 7}): ("F1", "Q1", "E1"),
            fingerprint({"limit": 11}): ("F2", "Q2", "E2"),
        }
        return digest, mapping[digest]

    def binding(self, event):
        if event not in {"E1", "E2"}:
            raise ValueError("invalid_event_tag")
        payload = encode(
            json.dumps(
                {
                    "run": self.run_uuid,
                    "event": event,
                    "exp": self.expires_at,
                    "nonce": self.nonces[event],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        signature = encode(
            hmac.new(self.signing_key.encode(), payload.encode(), hashlib.sha256).digest()
        )
        return payload + "." + signature

    def verify(self, binding, timestamp):
        if not isinstance(binding, str) or len(binding) > 512 or timestamp >= self.expires_at:
            raise ValueError("invalid_binding")
        for event in ("E1", "E2"):
            if hmac.compare_digest(binding.encode(), self.binding(event).encode()):
                return event, hashlib.sha256(binding.encode()).hexdigest()
        raise ValueError("invalid_binding")


def response(status, body=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": "" if body is None else json.dumps(body, separators=(",", ":")),
        "isBase64Encoded": False,
    }


def rpc_error(code, request_id=None):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": "method_not_supported" if code == -32601 else "invalid_request",
        },
    }


def handle(event, config, emit=print, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
    started = monotonic()
    timestamp = clock()
    headers = event.get("headers", {}) if isinstance(event, dict) else {}
    headers = headers if isinstance(headers, dict) else {}
    body = {}
    observation = {
        "run_uuid": config.run_uuid,
        "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat(),
        "latency_ms": 0,
        "method": "unsupported",
        "tool": "unsupported",
        "argument_fingerprint": None,
        "argument_tag": None,
        "request_tag": None,
        "binding_hash": None,
        "binding_tag": None,
        "verdict": "REJECTED",
    }

    def finish(status, result=None):
        observation["latency_ms"] = max(0, int((monotonic() - started) * 1000))
        observation["keypaths"], observation["headers"] = projection(body, headers)
        emit(json.dumps(observation, sort_keys=True, separators=(",", ":")))
        return response(status, result)

    if timestamp < config.issued_at or timestamp >= config.expires_at:
        return finish(410, {"error": "probe_expired"})
    authorization = header(headers, "Authorization")
    if not hmac.compare_digest(authorization.encode(), ("Bearer " + config.bearer).encode()):
        return finish(401, {"error": "unauthorized"})
    if any(isinstance(k, str) and k.lower() == "origin" for k in headers):
        return finish(403, {"error": "origin_not_allowed"})
    if event.get("httpMethod") != "POST":
        return finish(405)
    raw = event.get("body", "")
    if not isinstance(raw, str) or len(raw) > MAX_BODY * 2:
        return finish(413, {"error": "body_limit"})
    try:
        raw = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode()
        if len(raw) > MAX_BODY:
            return finish(413, {"error": "body_limit"})
        body = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return finish(400, rpc_error(-32602))
    if not isinstance(body, dict):
        return finish(400, rpc_error(-32602))
    method = body.get("method")
    observation["method"] = (
        method if isinstance(method, str) and method in METHODS else "unsupported"
    )
    request_id = body.get("id")
    valid_id = type(request_id) in (int, str) and (
        type(request_id) is int or len(request_id) <= 128
    )
    if (
        body.get("jsonrpc") != "2.0"
        or set(body) - {"jsonrpc", "id", "method", "params"}
        or not isinstance(method, str)
        or not isinstance(body.get("params", {}), dict)
    ):
        return finish(200, rpc_error(-32602))
    if method == "notifications/initialized" and "id" not in body:
        observation["verdict"] = "NOT_VERIFIED"
        return finish(202)
    if not valid_id:
        return finish(200, rpc_error(-32602))
    if method not in METHODS:
        return finish(200, rpc_error(-32601, request_id))
    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "chief-capture-probe", "version": "0.1.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "list_tasks",
                    "description": "Synthetic capture fixture.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "enum": [7, 11]}},
                        "required": ["limit"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
    elif method == "tools/call":
        params = body.get("params", {})
        observation["tool"] = "list_tasks" if params.get("name") == "list_tasks" else "unsupported"
        try:
            if set(params) != {"name", "arguments"} or params["name"] != "list_tasks":
                raise ValueError("invalid_tool")
            digest, (argument_tag, request_tag, expected_event) = config.expected(
                params["arguments"]
            )
        except ValueError:
            return finish(200, rpc_error(-32602, request_id))
        observation.update(
            argument_fingerprint=digest, argument_tag=argument_tag, request_tag=request_tag
        )
        try:
            binding_tag, binding_hash = config.verify(
                header(headers, "X-Spike-Event-Binding"), clock()
            )
        except ValueError:
            return finish(403, {"error": "binding_rejected"})
        observation.update(binding_tag=binding_tag, binding_hash=binding_hash)
        if binding_tag != expected_event:
            observation["verdict"] = "FAIL"
            return finish(403, {"error": "correlation_failed"})
        sleep(config.hold_ms / 1000)
        if clock() >= config.expires_at:
            return finish(410, {"error": "probe_expired"})
        result = {"content": [{"type": "text", "text": '{"items":[]}'}], "isError": False}
    else:
        return finish(200, rpc_error(-32601, request_id))
    # Even a matching fixture needs independent originating-provider-request evidence.
    observation["verdict"] = "NOT_VERIFIED"
    return finish(200, {"jsonrpc": "2.0", "id": request_id, "result": result})


def handler(event, context):
    # Only a fresh dedicated package asset is read; context tokens and environment are ignored.
    try:
        raw = Path(__file__).with_name("_probe_private.json").read_text()
        config = ProbeConfig(**json.loads(raw))
    except (OSError, ValueError, TypeError):
        return response(503, {"error": "probe_not_configured"})
    return handle(event, config)
