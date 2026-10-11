import hmac
import json
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse

from app.privacy import ContextChanged
from app.queue import LeaseLost
from app.spike_atelier.service import BindingRejected
from app.terminal import ExecutionExpired


class RPC(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jsonrpc: Literal["2.0"]
    id: int | str | None = None
    method: str
    params: dict = Field(default_factory=dict)


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: dict


def create_app(service):
    app = FastAPI(title="Isolated Atelier spike", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def reject_browser_origin(request, call_next):
        # Only server-to-server clients are supported by this isolated endpoint.
        if request.headers.get("origin"):
            return JSONResponse(status_code=403, content={"detail": "origin_not_allowed"})
        return await call_next(request)

    def authorize(supplied, expected):
        if not supplied or not hmac.compare_digest(supplied, expected.get_secret_value()):
            raise HTTPException(401, "unauthorized")
        try:
            service.require_binding_gate()
        except BindingRejected:
            raise HTTPException(503, "binding_not_verified") from None

    @app.post("/transport/updates")
    async def updates(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
        authorize(x_telegram_bot_api_secret_token, service.config.transport_secret)
        try:
            operation, binding = service.ingest(await request.json())
        except (BindingRejected, ValueError):
            raise HTTPException(403, "event_rejected") from None
        # The bridge retains binding as hidden per-event metadata, never as model arguments.
        return {"operation_id": str(operation), "event_binding": binding}

    @app.post("/mcp")
    async def mcp(
        request: Request,
        authorization: str = Header(default=""),
        x_spike_event_binding: str = Header(default=""),
    ):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "unauthorized")
        authorize(authorization[7:], service.config.mcp_secret)
        rpc = None
        try:
            rpc = RPC.model_validate(await request.json())
            if rpc.method == "notifications/initialized" and rpc.id is None:
                return Response(status_code=202)
            if rpc.id is None:
                raise ValueError("request_id_required")
            if rpc.method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "chief-atelier-spike", "version": "0.1.0"},
                }
            elif rpc.method == "tools/list":
                result = {"tools": service.schemas()}
            elif rpc.method == "tools/call":
                call = Call.model_validate(rpc.params)
                value = await service.call(x_spike_event_binding, call.name, call.arguments)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                value,
                                ensure_ascii=False,
                            ),
                        }
                    ],
                    "isError": value["status"] == "error",
                }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": rpc.id,
                    "error": {"code": -32601, "message": "method_not_supported"},
                }
            return {"jsonrpc": "2.0", "id": rpc.id, "result": result}
        except BindingRejected:
            raise HTTPException(403, "event_binding_rejected") from None
        except (ContextChanged, LeaseLost, ExecutionExpired):
            raise HTTPException(409, "event_retired") from None
        except (ValidationError, ValueError):
            # No submitted arguments, source text or credentials in error output.
            return {
                "jsonrpc": "2.0",
                "id": rpc.id if rpc else None,
                "error": {"code": -32602, "message": "invalid_request"},
            }

    return app
