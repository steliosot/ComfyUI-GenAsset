from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import numpy as np
import torch
from PIL import Image


CATEGORY = "genasset"


def _ensure_image_batch(image: torch.Tensor) -> torch.Tensor:
    if image.dim() == 3:
        image = image.unsqueeze(0)
    if image.dim() != 4:
        raise ValueError(f"Expected IMAGE tensor [B,H,W,C], got {tuple(image.shape)}")
    return image


def tensor_to_png_bytes(image: torch.Tensor) -> bytes:
    arr = image_to_rgb_array(image)
    pil = Image.fromarray((arr * 255.0).round().astype(np.uint8), mode="RGB")
    buffer = io.BytesIO()
    pil.save(buffer, format="PNG")
    return buffer.getvalue()


def image_to_rgb_array(image: torch.Tensor) -> np.ndarray:
    image = _ensure_image_batch(image)
    arr = image[0].detach().cpu().clamp(0, 1).numpy()
    if arr.shape[-1] == 1:
        arr = np.repeat(arr, 3, axis=-1)
    elif arr.shape[-1] == 2:
        arr = np.repeat(arr[..., :1], 3, axis=-1)
    elif arr.shape[-1] >= 4:
        rgb = arr[..., :3]
        alpha = arr[..., 3:4]
        arr = rgb * alpha + (1.0 - alpha)
    elif arr.shape[-1] != 3:
        raise ValueError(f"Expected IMAGE channels 1, 3, or 4; got {arr.shape[-1]}")
    return arr[..., :3]


def image_quality_metrics(image: torch.Tensor) -> dict[str, Any]:
    arr = image_to_rgb_array(image)
    luma = (0.2126 * arr[..., 0]) + (0.7152 * arr[..., 1]) + (0.0722 * arr[..., 2])
    mean = float(np.mean(luma))
    std = float(np.std(luma))
    p95 = float(np.percentile(luma, 95))
    p99 = float(np.percentile(luma, 99))
    non_black_fraction = float(np.mean(luma > 0.04))
    dynamic_range = float(np.max(luma) - np.min(luma))
    rejected = p99 < 0.04 or (mean < 0.025 and non_black_fraction < 0.02) or (std < 0.005 and mean < 0.04)
    return {
        "mean_luma": round(mean, 6),
        "std_luma": round(std, 6),
        "p95_luma": round(p95, 6),
        "p99_luma": round(p99, 6),
        "non_black_fraction": round(non_black_fraction, 6),
        "dynamic_range": round(dynamic_range, 6),
        "blank_or_black_rejected": rejected,
        "rule": "reject if p99<0.04, or mean<0.025 with <2% non-black pixels, or near-flat mean<0.04",
    }


def pil_to_tensor(image: Image.Image) -> torch.Tensor:
    arr = np.asarray(image.convert("RGB")).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


def blank_image() -> torch.Tensor:
    return torch.zeros((1, 64, 64, 3), dtype=torch.float32)


def parse_json(value: str, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def as_node_id(value: Any) -> str | None:
    if isinstance(value, (list, tuple)) and value:
        return str(value[0])
    if isinstance(value, str):
        return value
    return None


def input_link(node: dict[str, Any], name: str) -> str | None:
    inputs = node.get("inputs") if isinstance(node, dict) else {}
    if not isinstance(inputs, dict):
        return None
    return as_node_id(inputs.get(name))


def workflow_from_extra(extra_pnginfo: Any) -> dict[str, Any]:
    if isinstance(extra_pnginfo, dict):
        workflow = extra_pnginfo.get("workflow")
        return workflow if isinstance(workflow, dict) else {}
    return {}


def api_prompt_from_hidden(prompt: Any) -> dict[str, Any]:
    if isinstance(prompt, dict):
        return prompt
    return {}


def node_class(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("class_type") or node.get("type") or "")


def walk_upstream(api_prompt: dict[str, Any], start_id: str | None, limit: int = 80) -> list[str]:
    if not start_id:
        return []
    seen: set[str] = set()
    queue = [str(start_id)]
    ordered: list[str] = []
    while queue and len(ordered) < limit:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        node = api_prompt.get(node_id)
        if not isinstance(node, dict):
            continue
        ordered.append(node_id)
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for value in inputs.values():
            upstream = as_node_id(value)
            if upstream and upstream not in seen:
                queue.append(upstream)
    return ordered


def find_nodes_by_class(api_prompt: dict[str, Any], node_ids: list[str], class_names: set[str]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for node_id in node_ids:
        node = api_prompt.get(node_id)
        if isinstance(node, dict) and node_class(node) in class_names:
            out.append((node_id, node))
    return out


def find_first_node_by_class(api_prompt: dict[str, Any], node_ids: list[str], class_names: set[str]) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    matches = find_nodes_by_class(api_prompt, node_ids, class_names)
    return matches[0] if matches else (None, None)


def collect_prompt_texts(api_prompt: dict[str, Any], sampler: dict[str, Any] | None, upstream_ids: list[str]) -> tuple[str, str]:
    positive_text = ""
    negative_text = ""
    if sampler:
        positive_id = input_link(sampler, "positive")
        negative_id = input_link(sampler, "negative")
        positive_text = text_from_condition(api_prompt, positive_id)
        negative_text = text_from_condition(api_prompt, negative_id)

    if positive_text or negative_text:
        return positive_text, negative_text

    clip_nodes = find_nodes_by_class(api_prompt, upstream_ids, {"CLIPTextEncode", "CLIPTextEncodeSDXL", "BNK_CLIPTextEncodeAdvanced"})
    texts = []
    for _, node in clip_nodes:
        text = node.get("inputs", {}).get("text") if isinstance(node.get("inputs"), dict) else ""
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    if texts:
        positive_text = texts[0]
    if len(texts) > 1:
        negative_text = texts[1]
    return positive_text, negative_text


def text_from_condition(api_prompt: dict[str, Any], node_id: str | None) -> str:
    if not node_id:
        return ""
    node = api_prompt.get(str(node_id))
    if not isinstance(node, dict):
        return ""
    inputs = node.get("inputs")
    if isinstance(inputs, dict) and isinstance(inputs.get("text"), str):
        return inputs["text"]
    for upstream in walk_upstream(api_prompt, str(node_id), limit=20):
        upstream_node = api_prompt.get(upstream)
        if not isinstance(upstream_node, dict):
            continue
        upstream_inputs = upstream_node.get("inputs")
        if isinstance(upstream_inputs, dict) and isinstance(upstream_inputs.get("text"), str):
            return upstream_inputs["text"]
    return ""


def collect_checkpoint_name(api_prompt: dict[str, Any], upstream_ids: list[str]) -> str:
    for _, node in find_nodes_by_class(
        api_prompt,
        upstream_ids,
        {"CheckpointLoaderSimple", "CheckpointLoader", "UNETLoader", "DiffusersLoader", "VAELoader"},
    ):
        inputs = node.get("inputs") if isinstance(node, dict) else {}
        if not isinstance(inputs, dict):
            continue
        for key in ("ckpt_name", "checkpoint", "unet_name", "model_name", "vae_name"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def collect_sampler_metadata(sampler: dict[str, Any] | None) -> dict[str, Any]:
    inputs = sampler.get("inputs") if isinstance(sampler, dict) else {}
    if not isinstance(inputs, dict):
        return {}
    keys = ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise", "start_at_step", "end_at_step", "add_noise")
    return {key: inputs[key] for key in keys if key in inputs and not isinstance(inputs[key], (list, tuple))}


def collect_latent_metadata(api_prompt: dict[str, Any], upstream_ids: list[str]) -> dict[str, Any]:
    for _, node in find_nodes_by_class(api_prompt, upstream_ids, {"EmptyLatentImage", "EmptySD3LatentImage"}):
        inputs = node.get("inputs") if isinstance(node, dict) else {}
        if not isinstance(inputs, dict):
            continue
        out = {}
        for key in ("width", "height", "batch_size"):
            if key in inputs:
                out[key] = inputs[key]
        if out:
            return out
    return {}


def redact_secret_fields(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in {"workspace_token", "token", "authorization", "api_key", "secret", "service_role_key"}:
                out[key] = "[redacted]"
            else:
                out[key] = redact_secret_fields(item)
        return out
    if isinstance(value, list):
        return [redact_secret_fields(item) for item in value]
    return value


def derive_tags(prompt: str, model: str) -> list[str]:
    tags: list[str] = []
    lower = prompt.lower()
    for label in (
        "cinematic",
        "london",
        "widescreen",
        "portrait",
        "woman",
        "man",
        "robot",
        "rain",
        "spacecraft",
        "landscape",
        "house",
        "car",
        "product",
        "sdxl",
        "sd1.5",
    ):
        if re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", lower) and label not in tags:
            tags.append(label)
    model_lower = model.lower()
    if "xl" in model_lower and "sdxl" not in tags:
        tags.append("sdxl")
    if "realistic" in model_lower and "realistic" not in tags:
        tags.append("realistic")
    return tags[:8]


def generation_family(model: str) -> str:
    lower = model.lower()
    if "xl" in lower:
        return "sdxl"
    if "v1-5" in lower or "sd1" in lower or "realistic_vision" in lower:
        return "sd1.5"
    return ""


def auto_capture_generation(
    image: torch.Tensor,
    api_prompt: dict[str, Any],
    extra_pnginfo: Any,
    unique_id: Any,
    asset_name: str,
) -> dict[str, Any]:
    uid = str(unique_id[0] if isinstance(unique_id, (list, tuple)) and unique_id else unique_id or "")
    save_node = api_prompt.get(uid) if uid else None
    image_source_id = input_link(save_node, "image") if isinstance(save_node, dict) else None
    upstream_ids = walk_upstream(api_prompt, image_source_id)
    sampler_id, sampler = find_first_node_by_class(
        api_prompt,
        upstream_ids,
        {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"},
    )
    positive_prompt, negative_prompt = collect_prompt_texts(api_prompt, sampler, upstream_ids)
    sampler_metadata = collect_sampler_metadata(sampler)
    model = collect_checkpoint_name(api_prompt, upstream_ids)
    latent = collect_latent_metadata(api_prompt, upstream_ids)
    workflow = workflow_from_extra(extra_pnginfo)
    image_batch = _ensure_image_batch(image)
    height = int(image_batch.shape[1])
    width = int(image_batch.shape[2])
    seed = sampler_metadata.get("seed", 0)
    metadata = {
        "source": "comfyui-auto-capture",
        "asset_name": asset_name,
        "workflow": {
            "name": asset_name,
            "kind": "txt2img-from-comfyui",
            "family": generation_family(model),
        },
        "capture": {
            "node_id": uid,
            "image_source_node_id": image_source_id,
            "upstream_node_ids": upstream_ids,
        },
        "performance": sampler_metadata,
        "model": {
            "checkpoint": model,
        },
        "image": {
            "width": width,
            "height": height,
            "batch_size": int(image_batch.shape[0]),
        },
        "latent": latent,
    }
    workflow_json = {
        "api_prompt": redact_secret_fields(api_prompt),
        "workflow": redact_secret_fields(workflow),
        "captured_from_node_id": uid,
        "captured_upstream_node_ids": upstream_ids,
    }
    return {
        "prompt": positive_prompt,
        "negative_prompt": negative_prompt,
        "workflow_json": workflow_json,
        "model": model,
        "seed": int(seed) if isinstance(seed, int) or str(seed).isdigit() else 0,
        "metadata": metadata,
        "tags": derive_tags(positive_prompt, model),
        "sampler_id": sampler_id,
    }


def post_multipart(url: str, token: str, fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> dict[str, Any]:
    boundary = f"----GenAssetBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")

    for name, (filename, data, content_type) in files.items():
        safe_name = filename.replace('"', "")
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{safe_name}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        chunks.append(data)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    return read_json(request)


def read_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(payload)
        except Exception:
            data = {"error": payload}
        raise RuntimeError(data.get("error") or f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GenAsset: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("GenAsset returned invalid JSON.") from exc


def download_image(url: str) -> Image.Image:
    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-GenAsset/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    return Image.open(io.BytesIO(data)).convert("RGB")


def looks_like_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value.strip()))


DEFAULT_BASE_URL = "https://genasset.xyz"
WORKSPACE_TOKEN_PLACEHOLDER = "PASTE_TOKEN"


def require_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if not value:
        raise RuntimeError("Paste your GenAsset URL into base_url.")
    return value


def require_workspace_token(workspace_token: str) -> str:
    value = workspace_token.strip()
    if not value or value == WORKSPACE_TOKEN_PLACEHOLDER:
        raise RuntimeError("Paste your GenAsset workspace token into workspace_token.")
    return value


class GenAssetTestConnection:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "workspace_token": ("STRING", {"default": WORKSPACE_TOKEN_PLACEHOLDER, "multiline": False}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("workspace_name", "status_json")
    FUNCTION = "test"
    CATEGORY = CATEGORY

    def test(self, base_url: str, workspace_token: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token = require_workspace_token(workspace_token)
            url = urllib.parse.urljoin(clean_base_url + "/", "api/v1/workspace")
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {clean_workspace_token}"})
            data = read_json(request)
            workspace = data.get("workspace") or {}
            status = {
                "ok": True,
                "base_url": clean_base_url,
                "workspace": {
                    "id": workspace.get("id", ""),
                    "name": workspace.get("name", ""),
                    "slug": workspace.get("slug", ""),
                },
                "counts": data.get("counts") or {},
                "message": "Connected to GenAsset.",
            }
            return (str(workspace.get("name") or ""), json.dumps(status, indent=2))
        except Exception as exc:
            status = {
                "ok": False,
                "base_url": base_url.strip().rstrip("/"),
                "error": str(exc),
                "next_step": "Paste your GenAsset URL and workspace token, then run this node again.",
            }
            return ("", json.dumps(status, indent=2))


class GenAssetSaveGeneration:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "workspace_token": ("STRING", {"default": WORKSPACE_TOKEN_PLACEHOLDER, "multiline": False}),
                "asset_name": ("STRING", {"default": "Untitled asset"}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "asset_id", "version_id", "status_json")
    FUNCTION = "save"
    CATEGORY = CATEGORY

    def save(
        self,
        image: torch.Tensor,
        base_url: str,
        workspace_token: str,
        asset_name: str,
        prompt: Any = None,
        extra_pnginfo: Any = None,
        unique_id: Any = None,
    ):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token = require_workspace_token(workspace_token)
            api_prompt = api_prompt_from_hidden(prompt)
            quality = image_quality_metrics(image)
            if quality["blank_or_black_rejected"]:
                status = {
                    "saved": False,
                    "error": "Image looks blank or black, so GenAsset did not create a version.",
                    "image_quality": quality,
                    "next_step": "Rerun the sampler with a different seed, more steps, lower denoise, or a safer model/prompt before saving.",
                }
                return (image, "", "", json.dumps(status, indent=2))
            capture = auto_capture_generation(
                image=image,
                api_prompt=api_prompt,
                extra_pnginfo=extra_pnginfo,
                unique_id=unique_id,
                asset_name=asset_name,
            )
            capture["metadata"]["image_quality"] = quality
            capture["metadata"]["validation"] = {
                "black_image_guard": "passed",
                "black_image_guard_rule": quality["rule"],
            }
            fields = {
                "asset_name": asset_name,
                "asset_id": "",
                "prompt": capture["prompt"],
                "negative_prompt": capture["negative_prompt"],
                "workflow_json": json.dumps(capture["workflow_json"]),
                "model": capture["model"],
                "seed": str(capture["seed"]),
                "tags": ", ".join(capture.get("tags", [])),
                "intent": "",
                "metadata": json.dumps(capture["metadata"]),
                "source": "comfyui",
            }
            url = urllib.parse.urljoin(clean_base_url + "/", "api/v1/generations")
            data = post_multipart(
                url,
                clean_workspace_token,
                fields,
                {"image": ("generation.png", tensor_to_png_bytes(image), "image/png")},
            )
            out_asset_id = data.get("asset", {}).get("id", "")
            out_version_id = data.get("version", {}).get("id", "")
            return (image, out_asset_id, out_version_id, json.dumps(data, indent=2))
        except Exception as exc:
            status = {"error": str(exc)}
            return (image, "", "", json.dumps(status, indent=2))


class GenAssetLoadVersion:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "workspace_token": ("STRING", {"default": WORKSPACE_TOKEN_PLACEHOLDER, "multiline": False}),
                "version_id": ("STRING", {"default": ""}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "workflow_json", "metadata_json", "status_json")
    FUNCTION = "load"
    CATEGORY = CATEGORY

    def load(self, base_url: str, workspace_token: str, version_id: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token = require_workspace_token(workspace_token)
            if not version_id.strip():
                raise RuntimeError("Version id is required.")
            path = f"api/v1/versions/{urllib.parse.quote(version_id)}/load"
            url = urllib.parse.urljoin(clean_base_url + "/", path)
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {clean_workspace_token}"})
            data = read_json(request)
            version = data.get("version", {})
            preview_url = version.get("signed_preview_url")
            if not preview_url:
                raise RuntimeError("Version did not include a signed preview URL.")
            image = pil_to_tensor(download_image(preview_url))
            workflow_json = json.dumps(version.get("workflow_json") or {}, indent=2)
            metadata_json = json.dumps(version.get("metadata") or {}, indent=2)
            return (image, workflow_json, metadata_json, json.dumps(data, indent=2))
        except Exception as exc:
            status = {"error": str(exc)}
            return (blank_image(), "{}", "{}", json.dumps(status, indent=2))


class GenAssetLoadAsset:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "workspace_token": ("STRING", {"default": WORKSPACE_TOKEN_PLACEHOLDER, "multiline": False}),
                "asset_query": ("STRING", {"default": "", "tooltip": "Asset name, partial search text, or exact asset id."}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "asset_id", "version_id", "workflow_json", "metadata_json", "status_json")
    FUNCTION = "load"
    CATEGORY = CATEGORY

    def load(self, base_url: str, workspace_token: str, asset_query: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token = require_workspace_token(workspace_token)
            query_text = asset_query.strip()
            if not query_text:
                raise RuntimeError("Asset search text or asset id is required.")
            root = clean_base_url + "/"
            if looks_like_uuid(query_text):
                url = urllib.parse.urljoin(root, f"api/v1/assets/{urllib.parse.quote(query_text)}")
                request = urllib.request.Request(url, headers={"Authorization": f"Bearer {clean_workspace_token}"})
                data = read_json(request)
                asset = data.get("asset") or {}
                versions = data.get("versions") or []
                current_id = asset.get("current_version_id")
                version = next((item for item in versions if item.get("id") == current_id), versions[0] if versions else {})
            else:
                query = urllib.parse.urlencode({"search": query_text})
                url = urllib.parse.urljoin(root, f"api/v1/assets?{query}")
                request = urllib.request.Request(url, headers={"Authorization": f"Bearer {clean_workspace_token}"})
                data = read_json(request)
                assets = data.get("assets") or []
                if not assets:
                    raise RuntimeError(f"No GenAsset asset matched: {asset_query}")
                exact = next((item for item in assets if str(item.get("name", "")).lower() == query_text.lower()), None)
                asset = exact or assets[0]
                version = asset.get("current_version") or {}
            preview_url = version.get("signed_preview_url")
            if not preview_url:
                raise RuntimeError("Matched asset did not include a signed current-version preview URL.")
            image = pil_to_tensor(download_image(preview_url))
            workflow_json = json.dumps(version.get("workflow_json") or {}, indent=2)
            metadata_json = json.dumps(version.get("metadata") or {}, indent=2)
            status = {
                "asset": {
                    "id": asset.get("id", ""),
                    "name": asset.get("name", ""),
                    "version_count": asset.get("version_count", 0),
                },
                "version": {
                    "id": version.get("id", ""),
                    "version_number": version.get("version_number", 0),
                },
                "matched_query": asset_query,
                "load_mode": "asset_id" if looks_like_uuid(query_text) else "search",
            }
            return (
                image,
                str(asset.get("id", "")),
                str(version.get("id", "")),
                workflow_json,
                metadata_json,
                json.dumps(status, indent=2),
            )
        except Exception as exc:
            status = {"error": str(exc)}
            return (blank_image(), "", "", "{}", "{}", json.dumps(status, indent=2))
