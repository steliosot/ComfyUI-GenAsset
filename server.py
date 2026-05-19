from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path
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

    @routes.post("/genasset/setup/config")
    async def genasset_setup_config(request: web.Request) -> web.Response:
        blocked = _same_origin_block(request)
        if blocked is not None:
            return blocked
        payload = await _json_payload(request)
        try:
            from .nodes import (
                _genasset_config_paths,
                _read_genasset_config,
                genasset_manager_status,
                request_json,
                require_base_url,
            )

            base_url = require_base_url(str(payload.get("base_url") or "https://genasset.xyz"))
            workspace_token = str(payload.get("workspace_token") or "").strip()
            if not workspace_token:
                raise RuntimeError("Paste a GenAsset workspace token.")

            url = urllib.parse.urljoin(base_url + "/", "api/v1/workspace?lite=1&include_workspace_list=1")
            request_json("GET", url, workspace_token)

            try:
                config, existing_path = _read_genasset_config()
            except Exception:
                config, existing_path = {}, ""
            if not isinstance(config, dict):
                config = {}
            config.update({"base_url": base_url, "workspace_token": workspace_token})

            config_path = Path(existing_path) if existing_path else next(iter(_genasset_config_paths()))
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            try:
                os.chmod(config_path, 0o600)
            except Exception:
                pass

            return web.json_response(
                {
                    "ok": True,
                    "message": "GenAsset token saved.",
                    "config_path": str(config_path),
                    "status": genasset_manager_status(check=True),
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

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


def _same_origin_block(request: web.Request) -> web.Response | None:
    origin = str(request.headers.get("Origin") or request.headers.get("Referer") or "").strip()
    if not origin:
        return None
    try:
        origin_host = urllib.parse.urlparse(origin).netloc.lower()
    except Exception:
        origin_host = ""
    request_host = str(request.headers.get("Host") or getattr(request, "host", "") or "").lower()
    if origin_host and request_host and origin_host != request_host:
        return web.json_response(
            {"ok": False, "error": "Cross-origin GenAsset setup requests are not allowed."},
            status=403,
        )
    return None
