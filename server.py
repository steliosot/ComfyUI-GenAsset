from __future__ import annotations

import json
import urllib.parse
from typing import Any

from aiohttp import web

from .health import api_prompt_from_payload, build_health_payload, build_repro_lock, diagnose_workflow, resolve_models

_ROUTES_REGISTERED = False


def register_routes() -> None:
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED:
        return
    try:
        from server import PromptServer  # type: ignore
    except Exception:
        return

    routes = getattr(getattr(PromptServer, "instance", None), "routes", None)
    if routes is None:
        return
    _ROUTES_REGISTERED = True

    @routes.post("/genasset/health/resolve")
    async def genasset_health_resolve(request: web.Request) -> web.Response:
        payload = await _json_payload(request)
        api_prompt = api_prompt_from_payload(payload)
        return web.json_response(resolve_models(api_prompt))

    @routes.post("/genasset/health/repro")
    async def genasset_health_repro(request: web.Request) -> web.Response:
        payload = await _json_payload(request)
        api_prompt = api_prompt_from_payload(payload)
        workflow = payload.get("workflow") if isinstance(payload.get("workflow"), dict) else {}
        return web.json_response({"ok": True, "repro_lock": build_repro_lock(api_prompt, workflow)})

    @routes.post("/genasset/health/diagnose")
    async def genasset_health_diagnose(request: web.Request) -> web.Response:
        payload = await _json_payload(request)
        api_prompt = api_prompt_from_payload(payload)
        known_node_types = payload.get("known_node_types")
        if not isinstance(known_node_types, list):
            known_node_types = []
        return web.json_response(diagnose_workflow(api_prompt, known_node_types=[str(item) for item in known_node_types]))

    @routes.post("/genasset/health/doctor")
    async def genasset_health_doctor(request: web.Request) -> web.Response:
        payload = await _json_payload(request)
        try:
            from .nodes import request_json, require_base_url, resolve_workspace_token

            base_url = require_base_url(str(payload.get("base_url") or "https://genasset.xyz"))
            token_text = str(payload.get("token") or "ComfyUI/user/genasset.json")
            workspace_token, token_source, token_source_ref = resolve_workspace_token(token_text)
            health_payload = build_health_payload(payload)
            health_payload["client"] = {
                "source": "comfyui-genasset",
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            url = urllib.parse.urljoin(base_url + "/", "api/v1/comfy/workflow-doctor")
            data = request_json("POST", url, workspace_token, health_payload)
            return web.json_response(
                {
                    "ok": True,
                    "diagnostics": health_payload["diagnostics"],
                    "repro_lock": health_payload["repro_lock"],
                    "doctor": data,
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)


async def _json_payload(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text=f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Expected a JSON object.")
    return payload
