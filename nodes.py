from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image

from .health import build_repro_lock


CATEGORY = "genasset"
FilePart = tuple[str, bytes, str]
MAX_CATALOG_BYTES = 8 * 1024 * 1024
CATALOG_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,120}$")
RAW_GITHUB_PYPROJECT_URL = "https://raw.githubusercontent.com/steliosot/ComfyUI-GenAsset/main/pyproject.toml"
GENASSET_MANAGER_PACKAGE_ID = "genasset"
_GENASSET_UPDATE_LOCK = threading.Lock()


def _ensure_image_batch(image: torch.Tensor) -> torch.Tensor:
    if image.dim() == 3:
        image = image.unsqueeze(0)
    if image.dim() != 4:
        raise ValueError(f"Expected IMAGE tensor [B,H,W,C], got {tuple(image.shape)}")
    return image


def tensor_to_png_bytes(image: torch.Tensor) -> bytes:
    arr = image_to_rgb_array(image)
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
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
        negative_text = text_from_condition(api_prompt, negative_id, negative=True)

    if positive_text or negative_text:
        return positive_text, negative_text

    clip_nodes = find_nodes_by_class(
        api_prompt,
        upstream_ids,
        {
            "CLIPTextEncode",
            "CLIPTextEncodeSDXL",
            "BNK_CLIPTextEncodeAdvanced",
            "TextEncodeQwenImageEdit",
            "TextEncodeQwenImageEditPlus",
            "TextEncodeZImageOmni",
        },
    )
    texts = []
    for _, node in clip_nodes:
        text = prompt_text_from_inputs(node.get("inputs"))
        if text:
            texts.append(text)
    if texts:
        positive_text = texts[0]
    if len(texts) > 1:
        negative_text = texts[1]
    if positive_text or negative_text:
        return positive_text, negative_text
    return collect_prompt_texts_from_inputs(api_prompt, upstream_ids)


def prompt_text_from_inputs(inputs: Any) -> str:
    if not isinstance(inputs, dict):
        return ""
    for key in ("prompt", "positive", "text", "user_prompt", "prompt_text", "caption", "instruction", "instructions"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts = []
    for key in ("text_g", "text_l", "clip_l", "clip_g", "t5xxl", "llama"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " ".join(dict.fromkeys(parts)).strip()


def negative_prompt_text_from_inputs(inputs: Any) -> str:
    if not isinstance(inputs, dict):
        return ""
    for key in ("negative", "negative_prompt", "negative_prompt_text", "text_neg", "text_neg_g", "text_neg_l"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def collect_prompt_texts_from_inputs(api_prompt: dict[str, Any], upstream_ids: list[str]) -> tuple[str, str]:
    positive_text = ""
    negative_text = ""
    for node_id in upstream_ids:
        node = api_prompt.get(node_id)
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not positive_text:
            positive_text = prompt_text_from_inputs(inputs)
        if not negative_text:
            negative_text = negative_prompt_text_from_inputs(inputs)
        if positive_text and negative_text:
            break
    return positive_text, negative_text


def text_from_condition(api_prompt: dict[str, Any], node_id: str | None, negative: bool = False) -> str:
    if not node_id:
        return ""
    node = api_prompt.get(str(node_id))
    if not isinstance(node, dict):
        return ""
    if negative and node_class(node) in {"ConditioningZeroOut"}:
        return ""
    inputs = node.get("inputs")
    text = prompt_text_from_inputs(inputs)
    if text:
        return text
    for upstream in walk_upstream(api_prompt, str(node_id), limit=20):
        upstream_node = api_prompt.get(upstream)
        if not isinstance(upstream_node, dict):
            continue
        if negative and node_class(upstream_node) in {"ConditioningZeroOut"}:
            return ""
        upstream_inputs = upstream_node.get("inputs")
        text = prompt_text_from_inputs(upstream_inputs)
        if text:
            return text
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


def comfy_input_image_path(image_name: str) -> Path | None:
    if not image_name.strip():
        return None
    try:
        import folder_paths  # type: ignore

        path = Path(folder_paths.get_annotated_filepath(image_name))
        if path.is_file():
            return path
    except Exception:
        pass

    for base in (Path.cwd() / "input", Path.cwd()):
        path = base / image_name
        if path.is_file():
            return path
    return None


def collect_input_image_files(api_prompt: dict[str, Any], upstream_ids: list[str]) -> list[tuple[str, FilePart]]:
    files: list[tuple[str, FilePart]] = []
    seen: set[str] = set()
    for node_id, node in find_nodes_by_class(api_prompt, upstream_ids, {"LoadImage"}):
        inputs = node.get("inputs") if isinstance(node, dict) else {}
        image_name = pick_string(inputs.get("image") if isinstance(inputs, dict) else "")
        if not image_name or image_name in seen:
            continue
        seen.add(image_name)
        path = comfy_input_image_path(image_name)
        if not path:
            _log_error("SaveToGenAsset", f"Input image not found for upload: {image_name}")
            continue
        content_type = mimetypes.guess_type(path.name)[0] or "image/png"
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            _log_error("SaveToGenAsset", f"Skipping unsupported input image type: {image_name}")
            continue
        try:
            files.append(("input_images", (path.name, path.read_bytes(), content_type)))
            _log_ok("SaveToGenAsset", input_image=path.name, input_node=node_id)
        except Exception as exc:
            _log_error("SaveToGenAsset", f"Could not read input image {image_name}: {exc}")
    return files


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


def multipart_file_items(files: dict[str, FilePart] | list[tuple[str, FilePart]]) -> list[tuple[str, FilePart]]:
    if isinstance(files, dict):
        return list(files.items())
    return files


def post_multipart(url: str, token: str, fields: dict[str, str], files: dict[str, FilePart] | list[tuple[str, FilePart]]) -> dict[str, Any]:
    boundary = f"----GenAssetBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")

    for name, (filename, data, content_type) in multipart_file_items(files):
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
            data = {"error": friendly_non_json_error(request, payload, exc.code)}
        raise RuntimeError(data.get("error") or f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GenAsset: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(friendly_non_json_error(request, "", None)) from exc


def request_json(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    return read_json(request)


def friendly_non_json_error(request: urllib.request.Request, payload: str, status_code: int | None) -> str:
    url = getattr(request, "full_url", "")
    if "api/v1/comfy/workflow-assist" in url:
        base_url = url.split("/api/v1/comfy/workflow-assist", 1)[0]
        if "<html" in payload.lower() or "<!doctype" in payload.lower() or status_code == 404:
            return (
                "GenAsset Workflow Assistant is not available on this GenAsset server yet. "
                f"The ComfyUI node is installed, but {base_url}/api/v1/comfy/workflow-assist "
                "did not return the assistant API. Deploy the latest GenAsset app, or continue "
                "using Save To GenAsset manually."
            )
        return (
            "GenAsset Workflow Assistant returned a non-JSON response. "
            "Check that the GenAsset app is running the latest assistant API route."
        )
    return "GenAsset returned invalid JSON."


def _validate_catalog_workflow_id(workflow_id: str) -> str:
    value = str(workflow_id or "").strip()
    if not value or not CATALOG_ID_PATTERN.fullmatch(value):
        raise RuntimeError("Invalid GenAsset workflow id.")
    return value


def _read_public_json_url(url: str, max_bytes: int = MAX_CATALOG_BYTES) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-GenAsset/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > max_bytes:
                        raise RuntimeError("GenAsset workflow response is too large.")
                except ValueError:
                    pass
            payload = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(payload)
            message = data.get("error") if isinstance(data, dict) else ""
        except Exception:
            message = ""
        raise RuntimeError(message or f"GenAsset catalog returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GenAsset catalog: {exc.reason}") from exc

    if len(payload) > max_bytes:
        raise RuntimeError("GenAsset workflow response is too large.")
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("GenAsset catalog returned invalid JSON.") from exc


def _is_visual_workflow(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("nodes"), list) and len(value.get("nodes") or []) > 0


def _normalize_import_workflow_payload(payload: Any) -> dict[str, Any]:
    if _is_visual_workflow(payload):
        return payload
    if not isinstance(payload, dict):
        raise RuntimeError("GenAsset catalog response was not a workflow object.")
    workflow = payload.get("workflow")
    if _is_visual_workflow(workflow):
        return workflow
    workflow_json = payload.get("workflow_json")
    if _is_visual_workflow(workflow_json):
        return workflow_json
    if isinstance(workflow_json, dict) and _is_visual_workflow(workflow_json.get("workflow")):
        return workflow_json["workflow"]
    raise RuntimeError("GenAsset catalog response did not include a visual ComfyUI workflow.")


def _genasset_catalog_base_url() -> str:
    env_value = os.getenv("GENASSET_BASE_URL", "").strip().rstrip("/")
    if env_value:
        return env_value
    try:
        config, _ = _read_genasset_config()
        configured = pick_string(config.get("base_url")).rstrip("/")
        if configured:
            return configured
    except Exception:
        pass
    return DEFAULT_BASE_URL


def _catalog_url(path: str) -> str:
    base_url = _genasset_catalog_base_url()
    return urllib.parse.urljoin(base_url + "/", path.lstrip("/"))


def _slug_from_workflow_filename(file_name: str) -> str:
    text = re.sub(r"\.json$", "", file_name, flags=re.IGNORECASE)
    text = re.sub(r"^GenAsset-", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "workflow"


def _title_from_slug(slug: str) -> str:
    acronyms = {"ai", "api", "ui", "sdxl"}
    return " ".join(part.upper() if part in acronyms else part.capitalize() for part in slug.split("-") if part)


def _mounted_custom_workflow_dir() -> Path | None:
    configured = os.getenv("GENASSET_WORKFLOW_CATALOG_DIR", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            package_root() / "workflows",
            Path("/opt/ComfyUI/models/workflows/09_custom"),
            Path("/opt/ComfyUI/models/workflows"),
        ]
    )
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def _mounted_workflow_files() -> list[Path]:
    root = _mounted_custom_workflow_dir()
    if root is None:
        return []
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def _mounted_workflow_card(path: Path) -> dict[str, Any]:
    slug = _slug_from_workflow_filename(path.name)
    node_count = 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        workflow = _normalize_import_workflow_payload(payload)
        node_count = len(workflow.get("nodes") or [])
    except Exception:
        pass
    metadata_by_slug = {
        "import-demo": {
            "description": "Tiny no-model workflow for testing one-click GenAsset import into ComfyUI.",
            "category": "Setup",
            "needs_model": False,
            "model_requirements": [],
        },
        "simple-text-to-image-save": {
            "description": "Tiny text-to-image workflow that saves the result to GenAsset.",
            "category": "Starter",
            "needs_model": True,
            "model_requirements": ["Juggernaut_X_RunDiffusion.safetensors"],
        },
        "simple-text-to-image-ai-save": {
            "description": "Tiny text-to-image workflow where GenAsset Workflow Assistant fills save fields before saving.",
            "category": "Starter",
            "needs_model": True,
            "model_requirements": ["Juggernaut_X_RunDiffusion.safetensors"],
        },
    }
    metadata = metadata_by_slug.get(
        slug,
        {
            "description": "Ready-to-import GenAsset workflow.",
            "category": "Workflow",
            "needs_model": False,
            "model_requirements": [],
        },
    )
    return {
        "id": slug,
        "title": _title_from_slug(slug),
        "description": metadata["description"],
        "category": metadata["category"],
        "level": "Beginner",
        "node_count": node_count,
        "needs_model": metadata["needs_model"],
        "model_requirements": metadata["model_requirements"],
        "tags": ["custom"],
    }


def _mounted_public_workflow_catalog() -> dict[str, Any]:
    workflows = [_mounted_workflow_card(path) for path in _mounted_workflow_files()]
    if not workflows:
        raise RuntimeError("No mounted GenAsset workflows found.")
    return {"workflows": workflows}


def _mounted_public_import_workflow(workflow_id: str) -> dict[str, Any]:
    clean_id = _validate_catalog_workflow_id(workflow_id)
    for path in _mounted_workflow_files():
        slug = _slug_from_workflow_filename(path.name)
        if clean_id not in {slug, path.name, path.stem}:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        workflow = _normalize_import_workflow_payload(data)
        return {
            "ok": True,
            "id": slug,
            "title": _title_from_slug(slug),
            "workflow": workflow,
            "warnings": [],
            "source": str(path),
        }
    raise RuntimeError("Workflow not found.")


def fetch_public_workflow_catalog() -> dict[str, Any]:
    try:
        data = _read_public_json_url(_catalog_url("/api/catalog/workflow-import"))
        if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
            raise RuntimeError("GenAsset catalog did not return a workflow list.")
        return data
    except Exception:
        return _mounted_public_workflow_catalog()


def fetch_public_import_workflow(workflow_id: str) -> dict[str, Any]:
    clean_id = _validate_catalog_workflow_id(workflow_id)
    try:
        data = _read_public_json_url(_catalog_url(f"/api/catalog/workflow-import/{urllib.parse.quote(clean_id)}"))
        workflow = _normalize_import_workflow_payload(data)
        title = clean_id
        warnings: list[str] = []
        if isinstance(data, dict):
            title = pick_string(data.get("title"), data.get("id"), clean_id)
            raw_warnings = data.get("warnings")
            if isinstance(raw_warnings, list):
                warnings = [str(item) for item in raw_warnings if str(item).strip()]
        return {"ok": True, "id": clean_id, "title": title, "workflow": workflow, "warnings": warnings}
    except Exception:
        return _mounted_public_import_workflow(clean_id)


try:
    from aiohttp import web  # type: ignore
    from server import PromptServer  # type: ignore

    @PromptServer.instance.routes.get("/genasset/catalog/workflows")
    async def genasset_catalog_workflows_route(request):  # type: ignore[no-untyped-def]
        try:
            return web.json_response(fetch_public_workflow_catalog())
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=502)

    @PromptServer.instance.routes.get("/genasset/catalog/workflows/{workflow_id}")
    async def genasset_catalog_workflow_route(request):  # type: ignore[no-untyped-def]
        try:
            workflow_id = request.match_info.get("workflow_id", "")
            return web.json_response(fetch_public_import_workflow(workflow_id))
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=404)

except Exception:
    pass


def load_version_payload(base_url: str, workspace_token: str, version_id: str) -> dict[str, Any]:
    path = f"api/v1/versions/{urllib.parse.quote(version_id)}/load"
    url = urllib.parse.urljoin(base_url + "/", path)
    return request_json("GET", url, workspace_token)


def extract_prompt_pair_from_api_prompt(api_prompt: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(api_prompt, dict):
        return "", ""
    node_ids = list(api_prompt.keys())
    sampler_id, sampler = find_first_node_by_class(
        api_prompt,
        node_ids,
        {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"},
    )
    if sampler_id:
        upstream_ids = walk_upstream(api_prompt, sampler_id)
    else:
        upstream_ids = node_ids
    return collect_prompt_texts(api_prompt, sampler, upstream_ids)


def pick_dict(source: Any, key: str) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def pick_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ""


def pick_int(*values: Any) -> int:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text and re.fullmatch(r"-?\d+", text):
                return int(text)
    return 0


def pick_float(*values: Any) -> float:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                return float(text)
            except Exception:
                continue
    return 0.0


def download_image(url: str) -> Image.Image:
    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-GenAsset/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    return Image.open(io.BytesIO(data)).convert("RGB")


def looks_like_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value.strip()))


DEFAULT_BASE_URL = "https://genasset.xyz"
WORKSPACE_TOKEN_PLACEHOLDER = "PASTE_TOKEN"
GENASSET_CONFIG_FILENAME = "genasset.json"
TOKEN_FILE_HINT = "ComfyUI/user/genasset.json"


def _single_line(value: Any, limit: int = 220) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "..."
    return text


def _log_ok(node_name: str, **fields: Any) -> None:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        text = _single_line(value, limit=120)
        if text:
            parts.append(f"{key}={text}")
    suffix = f" {' '.join(parts)}" if parts else ""
    print(f"[GenAsset][{node_name}] OK{suffix}")


def _log_error(node_name: str, error: Any) -> None:
    print(f"[GenAsset][{node_name}] ERROR {_single_line(error)}")


def _genasset_config_paths() -> list[Path]:
    out: list[Path] = []
    env_path = os.getenv("GENASSET_CONFIG_PATH", "").strip()
    if env_path:
        out.append(Path(env_path).expanduser())
    try:
        import folder_paths  # type: ignore

        user_dir = getattr(folder_paths, "user_directory", "")
        if isinstance(user_dir, str) and user_dir.strip():
            out.append(Path(user_dir) / GENASSET_CONFIG_FILENAME)
    except Exception:
        pass
    out.append(Path.home() / "Documents" / "ComfyUI" / "user" / GENASSET_CONFIG_FILENAME)
    out.append(Path.cwd() / "user" / GENASSET_CONFIG_FILENAME)

    deduped: list[Path] = []
    seen: set[str] = set()
    for item in out:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _read_genasset_config() -> tuple[dict[str, Any], str]:
    for path in _genasset_config_paths():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid {GENASSET_CONFIG_FILENAME} at {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Invalid {GENASSET_CONFIG_FILENAME} at {path}: expected a JSON object.")
        return payload, str(path)
    return {}, ""


def resolve_workspace_token(token: str) -> tuple[str, str, str]:
    value = token.strip()
    if value and value not in {WORKSPACE_TOKEN_PLACEHOLDER, TOKEN_FILE_HINT}:
        return value, "widget", ""

    env_value = os.getenv("GENASSET_WORKSPACE_TOKEN", "").strip()
    if env_value:
        return env_value, "env", "GENASSET_WORKSPACE_TOKEN"

    config, config_path = _read_genasset_config()
    config_value = pick_string(config.get("workspace_token"), config.get("token"))
    if config_value:
        return config_value, "genasset.json", config_path

    raise RuntimeError(
        "Paste your GenAsset token into token, or set GENASSET_WORKSPACE_TOKEN, "
        "or create user/genasset.json with token or workspace_token."
    )


def package_root() -> Path:
    return Path(__file__).resolve().parent


def genasset_node_version() -> str:
    pyproject = package_root() / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except Exception:
        return "unknown"
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "unknown"


def genasset_last_updated_iso() -> str:
    candidates = [
        package_root() / "pyproject.toml",
        package_root() / "__init__.py",
        package_root() / "nodes.py",
        package_root() / "js" / "genasset_importer.js",
    ]
    mtimes = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime)
        except Exception:
            pass
    if not mtimes:
        return ""
    return datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat()


def version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


def compare_versions(current: str, latest: str) -> int:
    current_tuple = version_tuple(current)
    latest_tuple = version_tuple(latest)
    max_len = max(len(current_tuple), len(latest_tuple))
    current_tuple = current_tuple + (0,) * (max_len - len(current_tuple))
    latest_tuple = latest_tuple + (0,) * (max_len - len(latest_tuple))
    return (latest_tuple > current_tuple) - (latest_tuple < current_tuple)


def genasset_token_configured() -> tuple[bool, str, str]:
    if os.getenv("GENASSET_WORKSPACE_TOKEN", "").strip():
        return True, "env", "GENASSET_WORKSPACE_TOKEN"
    try:
        config, config_path = _read_genasset_config()
        if pick_string(config.get("workspace_token"), config.get("token")):
            return True, "genasset.json", config_path
    except Exception:
        return False, "", ""
    return False, "", ""


def genasset_manager_status(check: bool = False) -> dict[str, Any]:
    base_url = _genasset_catalog_base_url()
    token_configured, token_source, token_source_ref = genasset_token_configured()
    out: dict[str, Any] = {
        "ok": True,
        "name": "GenAsset Node",
        "version": genasset_node_version(),
        "last_updated": genasset_last_updated_iso(),
        "base_url": base_url,
        "token_configured": token_configured,
        "token_source": token_source,
        "token_source_ref": token_source_ref,
        "connection_checked": False,
        "connected": None,
        "api_reachable": None,
        "workspace_synced": None,
        "workspace": None,
        "organization": None,
        "workspaces": [],
        "counts": {},
    }
    if not check:
        return out
    out["connection_checked"] = True
    try:
        workspace_token, resolved_source, resolved_ref = resolve_workspace_token(TOKEN_FILE_HINT)
        out["token_source"] = resolved_source
        out["token_source_ref"] = resolved_ref
        url = urllib.parse.urljoin(base_url + "/", "api/v1/workspace?lite=1&include_workspace_list=1")
        data = request_json("GET", url, workspace_token)
        workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
        out.update(
            {
                "connected": True,
                "api_reachable": True,
                "workspace_synced": bool(workspace),
                "workspace": {
                    "id": str(workspace.get("id") or ""),
                    "name": str(workspace.get("name") or ""),
                    "slug": str(workspace.get("slug") or ""),
                },
                "organization": workspace.get("organization") if isinstance(workspace.get("organization"), dict) else None,
                "workspaces": workspace.get("workspaces") if isinstance(workspace.get("workspaces"), list) else [],
                "counts": data.get("counts") if isinstance(data.get("counts"), dict) else {},
            }
        )
    except Exception as exc:
        out.update({"ok": False, "connected": False, "api_reachable": False, "workspace_synced": False, "error": str(exc)})
    return out


def fetch_latest_genasset_node_version() -> dict[str, Any]:
    current = genasset_node_version()
    latest = ""
    try:
        text = urllib.request.urlopen(
            urllib.request.Request(RAW_GITHUB_PYPROJECT_URL, headers={"User-Agent": "ComfyUI-GenAsset/manager"}),
            timeout=20,
        ).read(256 * 1024).decode("utf-8", errors="replace")
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        latest = match.group(1) if match else ""
        if not latest:
            raise RuntimeError("Could not read latest version.")
        comparison = compare_versions(current, latest)
        return {
            "ok": True,
            "current_version": current,
            "latest_version": latest,
            "update_available": comparison > 0,
            "message": f"Update available: v{latest}" if comparison > 0 else "GenAsset is up to date.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "current_version": current,
            "latest_version": latest,
            "update_available": False,
            "error": str(exc),
            "message": "Could not check for updates.",
        }


def _candidate_comfyui_roots() -> list[Path]:
    roots: list[Path] = []
    for value in (os.getenv("COMFYUI_PATH"), os.getenv("COMFYUI_ROOT")):
        if value:
            roots.append(Path(value).expanduser())

    root = package_root()
    for parent in [root, *root.parents]:
        if parent.name == "custom_nodes" and parent.parent:
            roots.append(parent.parent)
        if (parent / "main.py").exists() and (parent / "custom_nodes").exists():
            roots.append(parent)

    try:
        cwd = Path.cwd()
        roots.append(cwd)
        if cwd.name == "custom_nodes" and cwd.parent:
            roots.append(cwd.parent)
    except Exception:
        pass

    unique: list[Path] = []
    seen: set[str] = set()
    for root_path in roots:
        try:
            resolved = root_path.resolve()
        except Exception:
            resolved = root_path
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def _find_manager_cli() -> tuple[Path, Path]:
    checked: list[str] = []
    manager_dir_names = ("ComfyUI-Manager", "ComfyUI-Manager-main", "comfyui-manager")
    for comfyui_root in _candidate_comfyui_roots():
        for manager_dir_name in manager_dir_names:
            candidate = comfyui_root / "custom_nodes" / manager_dir_name / "cm-cli.py"
            checked.append(str(candidate))
            if candidate.exists():
                return comfyui_root, candidate
    checked_text = "; ".join(checked[:8])
    raise RuntimeError(f"ComfyUI-Manager cm-cli.py was not found. Checked: {checked_text}")


def _run_manager_cli(args: list[str], comfyui_root: Path, manager_cli: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["COMFYUI_PATH"] = str(comfyui_root)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.run(
        [sys.executable, str(manager_cli), *args],
        cwd=str(comfyui_root),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
        check=False,
    )


def _tail_output(text: str, limit: int = 4000) -> str:
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[-limit:]


def _validate_update_version(version: str) -> str:
    clean = str(version or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?", clean):
        raise RuntimeError("Latest GenAsset version is not valid for an automatic update.")
    return clean


def perform_genasset_node_update() -> dict[str, Any]:
    if not _GENASSET_UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "updated": False, "error": "A GenAsset update is already running."}
    try:
        update = fetch_latest_genasset_node_version()
        current = str(update.get("current_version") or genasset_node_version())
        latest = str(update.get("latest_version") or "")
        if update.get("ok") and latest and compare_versions(current, latest) <= 0:
            return {
                "ok": True,
                "updated": False,
                "current_version": current,
                "latest_version": latest,
                "restart_required": False,
                "message": "GenAsset is already up to date.",
            }
        if not latest:
            raise RuntimeError(str(update.get("error") or "Could not determine the latest GenAsset version."))

        target_version = _validate_update_version(latest)
        comfyui_root, manager_cli = _find_manager_cli()
        attempts = [
            ["install", f"{GENASSET_MANAGER_PACKAGE_ID}@{target_version}", "--mode", "remote", "--channel", "default"],
            ["update", GENASSET_MANAGER_PACKAGE_ID, "--mode", "remote", "--channel", "default"],
        ]
        outputs: list[str] = []
        for args in attempts:
            result = _run_manager_cli(args, comfyui_root, manager_cli)
            output = _tail_output(result.stdout)
            outputs.append(f"$ {' '.join(args)}\n{output}")
            if result.returncode == 0:
                return {
                    "ok": True,
                    "updated": True,
                    "current_version": current,
                    "latest_version": target_version,
                    "restart_required": True,
                    "manager_cli": str(manager_cli),
                    "comfyui_root": str(comfyui_root),
                    "message": f"Updated GenAsset to v{target_version}. Restart ComfyUI to load it.",
                    "output": output,
                }

        raise RuntimeError("ComfyUI-Manager could not update GenAsset.\n" + "\n\n".join(outputs))
    except Exception as exc:
        return {
            "ok": False,
            "updated": False,
            "current_version": genasset_node_version(),
            "restart_required": False,
            "error": str(exc),
            "message": "GenAsset update failed.",
        }
    finally:
        _GENASSET_UPDATE_LOCK.release()


def workflow_name_from_version(version: dict[str, Any], fallback: str) -> str:
    metadata = version.get("metadata") if isinstance(version.get("metadata"), dict) else {}
    workflow_meta = metadata.get("workflow") if isinstance(metadata.get("workflow"), dict) else {}
    workflow_json = version.get("workflow_json") if isinstance(version.get("workflow_json"), dict) else {}
    return pick_string(workflow_meta.get("name"), workflow_json.get("name"), fallback)


def _display_name_from_email(value: str) -> str:
    clean = str(value or "").strip()
    if "@" not in clean:
        return clean
    local = clean.split("@", 1)[0]
    parts = [part for part in re.split(r"[._\-]+", local) if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts) or clean


def manager_actor_name(*values: Any) -> str:
    for value in values:
        if isinstance(value, dict):
            name = pick_string(
                value.get("display_name"),
                value.get("full_name"),
                value.get("name"),
                value.get("username"),
            )
            if name:
                return name
            email = pick_string(value.get("email"))
            if email:
                return _display_name_from_email(email)
        else:
            text = pick_string(value)
            if text:
                return _display_name_from_email(text)
    return ""


def compact_manager_asset(asset: dict[str, Any]) -> dict[str, Any]:
    current = asset.get("current_version") if isinstance(asset.get("current_version"), dict) else {}
    metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    current_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
    workflow_json = current.get("workflow_json") if isinstance(current.get("workflow_json"), dict) else {}
    node_count = 0
    importable = False
    try:
        workflow = _normalize_import_workflow_payload(workflow_json)
        node_count = len(workflow.get("nodes") or [])
        importable = True
    except Exception:
        pass
    return {
        "id": str(asset.get("id") or ""),
        "name": str(asset.get("name") or "Untitled asset"),
        "updated_at": str(asset.get("updated_at") or ""),
        "current_version_id": str((current or {}).get("id") or asset.get("current_version_id") or ""),
        "version_number": (current or {}).get("version_number"),
        "workflow_name": workflow_name_from_version(current, str(asset.get("name") or "Workflow")),
        "user_name": manager_actor_name(
            asset.get("updated_by"),
            asset.get("created_by"),
            asset.get("owner"),
            asset.get("user"),
            metadata.get("updated_by"),
            metadata.get("created_by"),
            metadata.get("owner"),
            metadata.get("user"),
            current.get("created_by"),
            current.get("user"),
            current_metadata.get("created_by"),
            current_metadata.get("user"),
        ),
        "workflow_importable": importable,
        "node_count": node_count,
    }


def genasset_manager_recent(page_size: int = 10, search: str = "") -> dict[str, Any]:
    base_url = _genasset_catalog_base_url()
    workspace_token = require_workspace_token(TOKEN_FILE_HINT)
    safe_search = str(search or "").strip()[:120]
    safe_size = max(1, min(50 if safe_search else 12, int(page_size or 10)))
    params: dict[str, str] = {"page_size": str(safe_size)}
    if safe_search:
        params["search"] = safe_search
    path = f"api/v1/assets?{urllib.parse.urlencode(params)}"
    data = request_json("GET", urllib.parse.urljoin(base_url + "/", path), workspace_token)
    raw_assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    assets = [compact_manager_asset(asset) for asset in raw_assets if isinstance(asset, dict)]
    workflows = [asset for asset in assets if asset.get("workflow_importable")]
    return {"assets": assets, "workflows": workflows, "pagination": data.get("pagination") or {}}


def genasset_manager_import_workspace_workflow(asset_id: str) -> dict[str, Any]:
    clean_asset_id = str(asset_id or "").strip()
    if not looks_like_uuid(clean_asset_id):
        raise RuntimeError("Invalid GenAsset asset id.")
    base_url = _genasset_catalog_base_url()
    workspace_token = require_workspace_token(TOKEN_FILE_HINT)
    path = f"api/v1/assets/{urllib.parse.quote(clean_asset_id)}?signed_artifacts=0"
    data = request_json("GET", urllib.parse.urljoin(base_url + "/", path), workspace_token)
    asset = data.get("asset") if isinstance(data.get("asset"), dict) else {}
    versions = data.get("versions") if isinstance(data.get("versions"), list) else []
    if not versions:
        raise RuntimeError("This asset has no versions.")
    current_id = str(asset.get("current_version_id") or "")
    version = next((item for item in versions if isinstance(item, dict) and str(item.get("id") or "") == current_id), None)
    if version is None:
        version = versions[0] if isinstance(versions[0], dict) else {}
    workflow = _normalize_import_workflow_payload(version.get("workflow_json") if isinstance(version, dict) else {})
    return {
        "ok": True,
        "id": clean_asset_id,
        "title": workflow_name_from_version(version, str(asset.get("name") or "GenAsset workflow")),
        "asset": {"id": str(asset.get("id") or clean_asset_id), "name": str(asset.get("name") or "")},
        "version": {"id": str(version.get("id") or ""), "version_number": version.get("version_number")},
        "workflow": workflow,
        "warnings": [],
    }


try:
    from aiohttp import web  # type: ignore
    from server import PromptServer  # type: ignore

    def _genasset_manager_origin_block(request):  # type: ignore[no-untyped-def]
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
                {"ok": False, "error": "Cross-origin GenAsset manager requests are not allowed."},
                status=403,
            )
        return None

    @PromptServer.instance.routes.get("/genasset/manager/status")
    async def genasset_manager_status_route(request):  # type: ignore[no-untyped-def]
        blocked = _genasset_manager_origin_block(request)
        if blocked is not None:
            return blocked
        check = request.query.get("check", "0") in {"1", "true", "yes"}
        try:
            return web.json_response(genasset_manager_status(check=check))
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @PromptServer.instance.routes.get("/genasset/manager/update-check")
    async def genasset_manager_update_check_route(request):  # type: ignore[no-untyped-def]
        blocked = _genasset_manager_origin_block(request)
        if blocked is not None:
            return blocked
        return web.json_response(fetch_latest_genasset_node_version())

    @PromptServer.instance.routes.post("/genasset/manager/update")
    async def genasset_manager_update_route(request):  # type: ignore[no-untyped-def]
        blocked = _genasset_manager_origin_block(request)
        if blocked is not None:
            return blocked
        return web.json_response(await asyncio.to_thread(perform_genasset_node_update))

    @PromptServer.instance.routes.get("/genasset/manager/recent")
    async def genasset_manager_recent_route(request):  # type: ignore[no-untyped-def]
        blocked = _genasset_manager_origin_block(request)
        if blocked is not None:
            return blocked
        try:
            page_size = pick_int(request.query.get("page_size"), 10)
            search = str(request.query.get("search") or "")
            return web.json_response(genasset_manager_recent(page_size=page_size, search=search))
        except Exception as exc:
            return web.json_response({"error": str(exc), "assets": [], "workflows": []}, status=502)

    @PromptServer.instance.routes.get("/genasset/manager/workspace-workflows/{asset_id}")
    async def genasset_manager_workspace_workflow_route(request):  # type: ignore[no-untyped-def]
        blocked = _genasset_manager_origin_block(request)
        if blocked is not None:
            return blocked
        try:
            asset_id = request.match_info.get("asset_id", "")
            return web.json_response(genasset_manager_import_workspace_workflow(asset_id))
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=404)

except Exception:
    pass


def key_loaded_note(token_source: str, token_source_ref: str) -> str:
    if token_source != "genasset.json":
        return ""
    display_ref = "ComfyUI/user/genasset.json"
    if token_source_ref:
        normalized = token_source_ref.replace("\\", "/")
        marker = "ComfyUI/user/genasset.json"
        if marker in normalized:
            display_ref = normalized[normalized.index(marker):]
        else:
            display_ref = normalized
    return f" Token loaded from {display_ref}."


def require_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if not value:
        raise RuntimeError("Paste your GenAsset URL into base_url.")
    return value


def require_workspace_token(workspace_token: str) -> str:
    value, _, _ = resolve_workspace_token(workspace_token)
    return value


def parse_tags_csv(value: str) -> list[str]:
    raw = [part.strip() for part in str(value or "").split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for part in raw:
        if not part:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return out


def merge_unique_tags(*tag_lists: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag_list in tag_lists:
        for tag in tag_list:
            text = str(tag or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out[:32]


def deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = deep_merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def compact_workflow_context(
    api_prompt: dict[str, Any],
    extra_pnginfo: Any,
    unique_id: Any,
    image: torch.Tensor | None = None,
    asset_name_hint: str = "",
    asset_id_hint: str = "",
) -> dict[str, Any]:
    uid = str(unique_id[0] if isinstance(unique_id, (list, tuple)) and unique_id else unique_id or "")
    assistant_node = api_prompt.get(uid) if uid else None
    image_source_id = input_link(assistant_node, "image") if isinstance(assistant_node, dict) else None
    if image_source_id:
        upstream_ids = walk_upstream(api_prompt, image_source_id)
    else:
        upstream_ids = list(api_prompt.keys())
    sampler_id, sampler = find_first_node_by_class(
        api_prompt,
        upstream_ids,
        {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"},
    )
    positive_prompt, negative_prompt = collect_prompt_texts(api_prompt, sampler, upstream_ids)
    sampler_metadata = collect_sampler_metadata(sampler)
    model = collect_checkpoint_name(api_prompt, upstream_ids)
    latent = collect_latent_metadata(api_prompt, upstream_ids)
    image_info: dict[str, Any] = {}
    if image is not None:
        try:
            image_batch = _ensure_image_batch(image)
            image_info = {
                "width": int(image_batch.shape[2]),
                "height": int(image_batch.shape[1]),
                "batch_size": int(image_batch.shape[0]),
            }
        except Exception:
            image_info = {}
    save_nodes = []
    genasset_nodes = []
    for node_id, node in api_prompt.items():
        if not isinstance(node, dict):
            continue
        klass = node_class(node)
        if klass.startswith("GenAsset"):
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            compact_inputs = {}
            for key, value in inputs.items():
                if str(key).lower() in {"token", "workspace_token", "authorization", "api_key", "secret"}:
                    compact_inputs[key] = "[redacted]"
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    compact_inputs[key] = value
                elif isinstance(value, (list, tuple)):
                    compact_inputs[key] = list(value[:2])
            record = {"id": str(node_id), "class_type": klass, "inputs": compact_inputs}
            genasset_nodes.append(record)
            if klass == "GenAssetSaveGeneration":
                save_nodes.append(record)
    workflow = workflow_from_extra(extra_pnginfo)
    return {
        "assistant_node_id": uid,
        "asset_name_hint": asset_name_hint.strip(),
        "asset_id_hint": asset_id_hint.strip(),
        "prompt": positive_prompt,
        "negative_prompt": negative_prompt,
        "model": model,
        "seed": sampler_metadata.get("seed", 0),
        "sampler": sampler_metadata,
        "latent": latent,
        "image": image_info,
        "capture": {
            "image_source_node_id": image_source_id,
            "sampler_node_id": sampler_id,
            "upstream_node_ids": upstream_ids,
        },
        "genasset_nodes": genasset_nodes,
        "save_nodes": save_nodes,
        "workflow_json": redact_secret_fields(
            {
                "api_prompt": api_prompt,
                "workflow": workflow,
                "captured_from_node_id": uid,
                "captured_upstream_node_ids": upstream_ids,
            }
        ),
        "auto_tags": derive_tags(positive_prompt, model),
    }


def compact_asset_row(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(asset.get("id") or ""),
        "name": str(asset.get("name") or ""),
        "tags": asset.get("tags") if isinstance(asset.get("tags"), list) else [],
        "current_version_id": str(asset.get("current_version_id") or ""),
        "updated_at": str(asset.get("updated_at") or ""),
        "created_at": str(asset.get("created_at") or ""),
    }


def compact_version_row(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(version.get("id") or ""),
        "asset_id": str(version.get("asset_id") or ""),
        "version_number": version.get("version_number"),
        "model": str(version.get("model") or ""),
        "seed": version.get("seed"),
        "tags": version.get("tags") if isinstance(version.get("tags"), list) else [],
        "created_at": str(version.get("created_at") or ""),
    }


class GenAssetTestConnection:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("workspace_name", "status_json", "summary", "state", "normalized_status_json")
    FUNCTION = "test"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def test(self, base_url: str, token: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
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
                "token_source": token_source,
                "token_source_ref": token_source_ref,
                "message": "Connected to GenAsset.",
            }
            workspace_name = str(workspace.get("name") or "")
            summary = (
                f"SUCCESS: Connected to workspace '{workspace_name}'."
                if workspace_name
                else "SUCCESS: Connected to GenAsset."
            )
            summary += key_loaded_note(token_source, token_source_ref)
            normalized = json.dumps(status, indent=2)
            return {"ui": {"text": [summary]}, "result": (workspace_name, normalized, summary, "success", normalized)}
        except Exception as exc:
            _log_error("TestGenAssetConnection", exc)
            status = {
                "ok": False,
                "base_url": base_url.strip().rstrip("/"),
                "error": str(exc),
                "next_step": "Paste your GenAsset URL and token, then run this node again.",
            }
            summary = f"ERROR: {status['error']}"
            normalized = json.dumps(status, indent=2)
            return {"ui": {"text": [summary]}, "result": ("", normalized, summary, "error", normalized)}


class GenAssetWorkflowAssistant:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
            },
            "optional": {
                "image": ("IMAGE",),
                "asset_name_hint": ("STRING", {"default": "", "tooltip": "Optional starting point for the suggested asset name."}),
                "asset_id_hint": ("STRING", {"default": "", "tooltip": "Optional existing asset id to prefer."}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = (
        "asset_name",
        "asset_id",
        "tags_csv",
        "intent",
        "notes_md",
        "version_label",
        "metadata_json",
        "warnings_json",
        "status_json",
        "summary",
    )
    FUNCTION = "assist"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def assist(
        self,
        base_url: str,
        token: str,
        image: torch.Tensor | None = None,
        asset_name_hint: str = "",
        asset_id_hint: str = "",
        prompt: Any = None,
        extra_pnginfo: Any = None,
        unique_id: Any = None,
    ):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            api_prompt = api_prompt_from_hidden(prompt)
            context = compact_workflow_context(
                api_prompt=api_prompt,
                extra_pnginfo=extra_pnginfo,
                unique_id=unique_id,
                image=image,
                asset_name_hint=asset_name_hint,
                asset_id_hint=asset_id_hint,
            )
            context["client"] = {
                "source": "comfyui-genasset",
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            url = urllib.parse.urljoin(clean_base_url + "/", "api/v1/comfy/workflow-assist")
            data = request_json("POST", url, clean_workspace_token, context)
            suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), dict) else {}
            warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
            asset_name = str(suggestions.get("asset_name") or data.get("asset_name") or asset_name_hint or "Untitled asset").strip()
            asset_id = str(suggestions.get("asset_id") or asset_id_hint or "").strip()
            tags = suggestions.get("tags")
            tags_csv = ", ".join(str(tag).strip() for tag in tags if str(tag).strip()) if isinstance(tags, list) else str(suggestions.get("tags_csv") or "")
            intent = str(suggestions.get("intent") or "").strip()
            notes_md = str(suggestions.get("notes_md") or "").strip()
            version_label = str(suggestions.get("version_label") or "").strip()
            metadata = suggestions.get("metadata") if isinstance(suggestions.get("metadata"), dict) else {}
            metadata_json = json.dumps(metadata, indent=2)
            warnings_json = json.dumps(warnings, indent=2)
            status = {
                "ok": bool(data.get("ok", True)),
                "source": data.get("source") or "genasset",
                "workspace": data.get("workspace") or {},
                "warning_count": len(warnings),
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            summary = str(data.get("summary") or f"Suggested asset name: {asset_name}").strip()
            if token_source == "genasset.json":
                summary += key_loaded_note(token_source, token_source_ref)
            status_json = json.dumps(status, indent=2)
            return {
                "ui": {"text": [summary, warnings_json]},
                "result": (asset_name, asset_id, tags_csv, intent, notes_md, version_label, metadata_json, warnings_json, status_json, summary),
            }
        except Exception as exc:
            _log_error("WorkflowAssistant", exc)
            status = {"ok": False, "error": str(exc)}
            status_json = json.dumps(status, indent=2)
            summary = f"ERROR: {str(exc)}"
            return {
                "ui": {"text": [summary]},
                "result": ("", "", "", "", "", "", "{}", "[]", status_json, summary),
            }


class GenAssetSaveGeneration:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_name": ("STRING", {"default": "Untitled asset"}),
            },
            "optional": {
                "asset_id": ("STRING", {"default": "", "tooltip": "Optional. Save to this existing GenAsset asset id."}),
                "tags_csv": ("STRING", {"default": "", "tooltip": "Optional. Extra tags to merge with auto-captured tags."}),
                "intent": ("STRING", {"default": "", "tooltip": "Optional. Workflow intent, for example txt2img, img2img, inpaint, or upscale."}),
                "notes_md": ("STRING", {"default": "", "multiline": True, "tooltip": "Optional. Asset-level notes to save with this generation."}),
                "version_label": ("STRING", {"default": "", "tooltip": "Optional. Short label stored in version metadata."}),
                "metadata_json": ("STRING", {"default": "{}", "multiline": True, "tooltip": "Optional. Extra JSON metadata merged into captured metadata."}),
                "source": ("STRING", {"default": "comfyui", "tooltip": "Optional source label. Defaults to comfyui."}),
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
        token: str,
        asset_name: str,
        asset_id: str = "",
        tags_csv: str = "",
        intent: str = "",
        notes_md: str = "",
        version_label: str = "",
        metadata_json: str = "{}",
        source: str = "comfyui",
        prompt: Any = None,
        extra_pnginfo: Any = None,
        unique_id: Any = None,
    ):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, _ = resolve_workspace_token(token)
            api_prompt = api_prompt_from_hidden(prompt)
            quality = image_quality_metrics(image)
            if quality["blank_or_black_rejected"]:
                status = {
                    "saved": False,
                    "error": "Image looks blank or black, so GenAsset did not create a version.",
                    "image_quality": quality,
                    "next_step": "Rerun the sampler with a different seed, more steps, lower denoise, or a safer model/prompt before saving.",
                }
                _log_error("SaveToGenAsset", status["error"])
                return (image, "", "", json.dumps(status, indent=2))
            capture = auto_capture_generation(
                image=image,
                api_prompt=api_prompt,
                extra_pnginfo=extra_pnginfo,
                unique_id=unique_id,
                asset_name=asset_name,
            )
            capture["metadata"]["repro_lock"] = build_repro_lock(api_prompt, workflow_from_extra(extra_pnginfo))
            capture["metadata"]["image_quality"] = quality
            capture["metadata"]["validation"] = {
                "black_image_guard": "passed",
                "black_image_guard_rule": quality["rule"],
            }
            extra_metadata = parse_json(metadata_json, {})
            if not isinstance(extra_metadata, dict):
                extra_metadata = {}
            label_text = version_label.strip()
            notes_text = notes_md.strip()
            assistant_metadata = {}
            if label_text:
                assistant_metadata["version_label"] = label_text
            if notes_text:
                assistant_metadata["notes_md"] = notes_text
            if assistant_metadata:
                extra_metadata = deep_merge_dict(extra_metadata, {"genasset_assistant": assistant_metadata})
            if extra_metadata:
                capture["metadata"] = deep_merge_dict(capture["metadata"], extra_metadata)
            tags = merge_unique_tags(capture.get("tags", []), parse_tags_csv(tags_csv))
            input_image_files = collect_input_image_files(api_prompt, capture["metadata"]["capture"]["upstream_node_ids"])
            fields = {
                "asset_name": asset_name,
                "asset_id": asset_id.strip(),
                "prompt": capture["prompt"],
                "negative_prompt": capture["negative_prompt"],
                "workflow_json": json.dumps(capture["workflow_json"]),
                "model": capture["model"],
                "seed": str(capture["seed"]),
                "tags": ", ".join(tags),
                "intent": intent.strip(),
                "metadata": json.dumps(capture["metadata"]),
                "source": source.strip() or "comfyui",
                "notes_md": notes_text,
                "version_label": label_text,
            }
            url = urllib.parse.urljoin(clean_base_url + "/", "api/v1/generations")
            data = post_multipart(
                url,
                clean_workspace_token,
                fields,
                [("image", ("generation.png", tensor_to_png_bytes(image), "image/png")), *input_image_files],
            )
            out_asset_id = data.get("asset", {}).get("id", "")
            out_version_id = data.get("version", {}).get("id", "")
            _log_ok("SaveToGenAsset", asset_id=out_asset_id, version_id=out_version_id, token_source=token_source)
            return (image, out_asset_id, out_version_id, json.dumps(data, indent=2))
        except Exception as exc:
            _log_error("SaveToGenAsset", exc)
            status = {"error": str(exc)}
            return (image, "", "", json.dumps(status, indent=2))


class GenAssetLoadVersion:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Optional. Exact asset id. Empty means latest asset in this workspace.",
                    },
                ),
                "version_id": (
                    "STRING",
                    {"default": "", "tooltip": "Optional. When set, this exact version id is loaded."},
                ),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "asset_id", "version_id", "workflow_json", "metadata_json", "status_json")
    FUNCTION = "load"
    CATEGORY = CATEGORY

    def load(self, base_url: str, token: str, asset_id: str, version_id: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            asset_id_text = asset_id.strip()
            explicit_version_id = version_id.strip()
            root = clean_base_url + "/"
            load_mode = "latest"
            selected_asset_id = ""
            selected_version_id = ""

            if explicit_version_id:
                load_mode = "version_id"
                path = f"api/v1/versions/{urllib.parse.quote(explicit_version_id)}/load"
                url = urllib.parse.urljoin(root, path)
                request = urllib.request.Request(url, headers={"Authorization": f"Bearer {clean_workspace_token}"})
                data = read_json(request)
                version = data.get("version") or {}
                selected_asset_id = str((data.get("asset") or {}).get("id") or version.get("asset_id") or "")
                selected_version_id = str(version.get("id") or explicit_version_id)
            else:
                if asset_id_text:
                    load_mode = "asset_id"
                    asset_url = urllib.parse.urljoin(root, f"api/v1/assets/{urllib.parse.quote(asset_id_text)}")
                    asset_request = urllib.request.Request(asset_url, headers={"Authorization": f"Bearer {clean_workspace_token}"})
                    asset_data = read_json(asset_request)
                    asset = asset_data.get("asset") or {}
                    versions = asset_data.get("versions") or []
                    current_id = asset.get("current_version_id")
                    version = next((item for item in versions if item.get("id") == current_id), versions[0] if versions else {})
                    selected_asset_id = str(asset.get("id") or asset_id_text)
                else:
                    load_mode = "latest"
                    path = "api/v1/assets"
                    assets_url = urllib.parse.urljoin(root, path)
                    assets_request = urllib.request.Request(assets_url, headers={"Authorization": f"Bearer {clean_workspace_token}"})
                    assets_data = read_json(assets_request)
                    assets = assets_data.get("assets") or []
                    if not assets:
                        raise RuntimeError("No assets found in this workspace.")
                    asset = assets[0]
                    version = asset.get("current_version") or {}
                    selected_asset_id = str(asset.get("id", ""))

                selected_version_id = str(version.get("id", ""))
                data = {
                    "asset": {"id": selected_asset_id, "name": str(asset.get("name", "")), "version_count": asset.get("version_count", 0)},
                    "version": {
                        "id": selected_version_id,
                        "version_number": version.get("version_number", 0),
                        "workflow_json": version.get("workflow_json"),
                        "metadata": version.get("metadata"),
                    },
                }

            preview_url = (data.get("version") or {}).get("signed_preview_url") or version.get("signed_preview_url")
            if not preview_url:
                raise RuntimeError("Matched version did not include a signed preview URL.")
            image = pil_to_tensor(download_image(preview_url))
            workflow_json = json.dumps((data.get("version") or {}).get("workflow_json") or version.get("workflow_json") or {}, indent=2)
            metadata_json = json.dumps((data.get("version") or {}).get("metadata") or version.get("metadata") or {}, indent=2)
            status = {
                "asset": {"id": selected_asset_id},
                "version": {"id": selected_version_id},
                "matched_asset_id": asset_id_text,
                "load_mode": load_mode,
                "token_source": token_source,
                "token_source_ref": token_source_ref,
                "defaults": {"version_id_optional": True, "asset_id_optional": True, "empty_asset_id_loads": "latest_asset"},
            }
            return (
                image,
                selected_asset_id,
                selected_version_id,
                workflow_json,
                metadata_json,
                json.dumps(status, indent=2),
            )
        except Exception as exc:
            _log_error("LoadAssetFromGenAsset", exc)
            status = {"error": str(exc)}
            return (blank_image(), "", "", "{}", "{}", json.dumps(status, indent=2))


class GenAssetLoadExactVersion:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "version_id": ("STRING", {"default": "", "tooltip": "Required. Exact GenAsset version id (UUID)."}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "asset_id", "version_id", "workflow_json", "metadata_json", "status_json")
    FUNCTION = "load_exact"
    CATEGORY = CATEGORY

    def load_exact(self, base_url: str, token: str, version_id: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            explicit_version_id = version_id.strip()
            if not explicit_version_id:
                raise RuntimeError("Paste a version_id to load one exact version.")

            data = load_version_payload(clean_base_url, clean_workspace_token, explicit_version_id)
            version = data.get("version") or {}
            preview_url = version.get("signed_preview_url")
            if not preview_url:
                raise RuntimeError("Matched version did not include a signed preview URL.")
            image = pil_to_tensor(download_image(preview_url))
            asset_id = str((data.get("asset") or {}).get("id") or version.get("asset_id") or "")
            version_out = str(version.get("id") or explicit_version_id)
            workflow_json = json.dumps(version.get("workflow_json") or {}, indent=2)
            metadata_json = json.dumps(version.get("metadata") or {}, indent=2)
            status = {
                "asset": {"id": asset_id},
                "version": {"id": version_out},
                "load_mode": "exact_version",
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            return (image, asset_id, version_out, workflow_json, metadata_json, json.dumps(status, indent=2))
        except Exception as exc:
            _log_error("LoadVersionFromGenAsset", exc)
            status = {"error": str(exc)}
            return (blank_image(), "", "", "{}", "{}", json.dumps(status, indent=2))


class GenAssetPatchVersionMetadata:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "version_id": ("STRING", {"default": "", "tooltip": "Version id to patch."}),
                "metadata_patch_json": (
                    "STRING",
                    {"default": "{\n  \"approval\": \"approved\"\n}", "multiline": True},
                ),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("version_id", "metadata_json", "status_json")
    FUNCTION = "patch"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def patch(self, base_url: str, token: str, version_id: str, metadata_patch_json: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            version_text = version_id.strip()
            if not version_text:
                raise RuntimeError("Paste a version_id to patch metadata.")
            metadata_patch = parse_json(metadata_patch_json, None)
            if not isinstance(metadata_patch, dict):
                raise RuntimeError("metadata_patch_json must be a JSON object.")
            path = f"api/v1/versions/{urllib.parse.quote(version_text)}/metadata"
            url = urllib.parse.urljoin(clean_base_url + "/", path)
            data = request_json("PATCH", url, clean_workspace_token, {"metadata": metadata_patch})
            version = data.get("version") or {}
            metadata_json = json.dumps(version.get("metadata") or {}, indent=2)
            status = {
                "ok": True,
                "version": {"id": str(version.get("id") or version_text)},
                "patched_keys": sorted(metadata_patch.keys()),
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            summary = f"SUCCESS: Metadata patched for version {str(version.get('id') or version_text)}."
            summary += key_loaded_note(token_source, token_source_ref)
            status_json = json.dumps(status, indent=2)
            _log_ok("SaveMetadataPatchToGenAsset", version_id=str(version.get("id") or version_text))
            return {"ui": {"text": [summary]}, "result": (str(version.get("id") or version_text), metadata_json, status_json)}
        except Exception as exc:
            _log_error("SaveMetadataPatchToGenAsset", exc)
            status = {"ok": False, "error": str(exc)}
            summary = f"ERROR: {status['error']}"
            status_json = json.dumps(status, indent=2)
            return {"ui": {"text": [summary]}, "result": ("", "{}", status_json)}


class GenAssetCompareVersions:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "left_version_id": ("STRING", {"default": "", "tooltip": "Left/older version id."}),
                "right_version_id": ("STRING", {"default": "", "tooltip": "Right/newer version id."}),
            }
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("left_image", "right_image", "diff_json", "status_json")
    FUNCTION = "compare"
    CATEGORY = CATEGORY

    def compare(self, base_url: str, token: str, left_version_id: str, right_version_id: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            left_id = left_version_id.strip()
            right_id = right_version_id.strip()
            if not left_id or not right_id:
                raise RuntimeError("Paste both left_version_id and right_version_id.")

            left_data = load_version_payload(clean_base_url, clean_workspace_token, left_id)
            right_data = load_version_payload(clean_base_url, clean_workspace_token, right_id)
            left_version = left_data.get("version") or {}
            right_version = right_data.get("version") or {}
            left_preview = left_version.get("signed_preview_url")
            right_preview = right_version.get("signed_preview_url")
            if not left_preview or not right_preview:
                raise RuntimeError("One or both versions did not include a signed preview URL.")

            left_image = pil_to_tensor(download_image(left_preview))
            right_image = pil_to_tensor(download_image(right_preview))

            left_meta = left_version.get("metadata") if isinstance(left_version.get("metadata"), dict) else {}
            right_meta = right_version.get("metadata") if isinstance(right_version.get("metadata"), dict) else {}
            left_perf = left_meta.get("performance") if isinstance(left_meta, dict) and isinstance(left_meta.get("performance"), dict) else {}
            right_perf = right_meta.get("performance") if isinstance(right_meta, dict) and isinstance(right_meta.get("performance"), dict) else {}
            left_model = pick_string(left_version.get("model"), pick_string(pick_dict(left_meta, "model").get("checkpoint")))
            right_model = pick_string(right_version.get("model"), pick_string(pick_dict(right_meta, "model").get("checkpoint")))

            diff = {
                "left": {
                    "version_id": str(left_version.get("id") or left_id),
                    "version_number": left_version.get("version_number"),
                    "asset_id": left_version.get("asset_id"),
                    "prompt": left_version.get("prompt"),
                    "negative_prompt": left_version.get("negative_prompt"),
                    "model": left_model,
                    "seed": left_version.get("seed"),
                    "sampler_name": left_perf.get("sampler_name"),
                    "scheduler": left_perf.get("scheduler"),
                    "steps": left_perf.get("steps"),
                    "cfg": left_perf.get("cfg"),
                    "denoise": left_perf.get("denoise"),
                },
                "right": {
                    "version_id": str(right_version.get("id") or right_id),
                    "version_number": right_version.get("version_number"),
                    "asset_id": right_version.get("asset_id"),
                    "prompt": right_version.get("prompt"),
                    "negative_prompt": right_version.get("negative_prompt"),
                    "model": right_model,
                    "seed": right_version.get("seed"),
                    "sampler_name": right_perf.get("sampler_name"),
                    "scheduler": right_perf.get("scheduler"),
                    "steps": right_perf.get("steps"),
                    "cfg": right_perf.get("cfg"),
                    "denoise": right_perf.get("denoise"),
                },
                "changed": {
                    "prompt": left_version.get("prompt") != right_version.get("prompt"),
                    "negative_prompt": left_version.get("negative_prompt") != right_version.get("negative_prompt"),
                    "model": left_model != right_model,
                    "seed": left_version.get("seed") != right_version.get("seed"),
                    "sampler_name": left_perf.get("sampler_name") != right_perf.get("sampler_name"),
                    "scheduler": left_perf.get("scheduler") != right_perf.get("scheduler"),
                    "steps": left_perf.get("steps") != right_perf.get("steps"),
                    "cfg": left_perf.get("cfg") != right_perf.get("cfg"),
                    "denoise": left_perf.get("denoise") != right_perf.get("denoise"),
                },
            }
            status = {
                "ok": True,
                "left_version_id": str(left_version.get("id") or left_id),
                "right_version_id": str(right_version.get("id") or right_id),
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            return (left_image, right_image, json.dumps(diff, indent=2), json.dumps(status, indent=2))
        except Exception as exc:
            _log_error("CompareTwoGenAssetVersions", exc)
            status = {"ok": False, "error": str(exc)}
            return (blank_image(), blank_image(), "{}", json.dumps(status, indent=2))


class GenAssetCreateBranchVersion:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_id": ("STRING", {"default": "", "tooltip": "Target asset id to append a branched version."}),
                "parent_version_id": ("STRING", {"default": "", "tooltip": "Parent/source version id for this branch."}),
                "asset_name": ("STRING", {"default": "", "tooltip": "Optional fallback if asset_id is empty."}),
                "prompt_text": ("STRING", {"default": "", "multiline": True}),
                "negative_prompt_text": ("STRING", {"default": "", "multiline": True}),
                "model_name": ("STRING", {"default": ""}),
                "seed": ("INT", {"default": 0, "min": 0}),
                "tags_csv": ("STRING", {"default": ""}),
                "intent": ("STRING", {"default": "branch_edit"}),
                "extra_metadata_json": ("STRING", {"default": "{}", "multiline": True}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "asset_id", "version_id", "status_json")
    FUNCTION = "create"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def create(
        self,
        image: torch.Tensor,
        base_url: str,
        token: str,
        asset_id: str,
        parent_version_id: str,
        asset_name: str,
        prompt_text: str,
        negative_prompt_text: str,
        model_name: str,
        seed: int,
        tags_csv: str,
        intent: str,
        extra_metadata_json: str,
    ):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            asset_id_text = asset_id.strip()
            parent_version_text = parent_version_id.strip()
            if not parent_version_text:
                raise RuntimeError("Paste parent_version_id to create a branch version.")
            if parent_version_text == "PASTE_PARENT_VERSION_ID":
                raise RuntimeError("Replace PASTE_PARENT_VERSION_ID with a real parent version id.")
            if not looks_like_uuid(parent_version_text):
                raise RuntimeError("parent_version_id must be a version UUID.")
            if not asset_id_text and not asset_name.strip():
                raise RuntimeError("Set asset_id or asset_name for branch save.")
            if asset_id_text and not looks_like_uuid(asset_id_text):
                raise RuntimeError("asset_id must be an asset UUID.")

            extra_meta = parse_json(extra_metadata_json, {})
            if not isinstance(extra_meta, dict):
                raise RuntimeError("extra_metadata_json must be a JSON object.")
            metadata = {
                **extra_meta,
                "branch": {
                    "parent_version_id": parent_version_text,
                    "created_in": "comfyui",
                },
            }
            fields = {
                "asset_name": asset_name.strip() or "Untitled asset",
                "asset_id": asset_id_text,
                "prompt": prompt_text,
                "negative_prompt": negative_prompt_text,
                "workflow_json": json.dumps({}),
                "model": model_name.strip(),
                "seed": str(int(seed)),
                "tags": tags_csv,
                "intent": intent.strip() or "branch_edit",
                "metadata": json.dumps(metadata),
                "source": "comfyui-branch",
            }
            url = urllib.parse.urljoin(clean_base_url + "/", "api/v1/generations")
            data = post_multipart(
                url,
                clean_workspace_token,
                fields,
                {"image": ("branch_generation.png", tensor_to_png_bytes(image), "image/png")},
            )
            out_asset_id = str((data.get("asset") or {}).get("id") or asset_id_text)
            out_version_id = str((data.get("version") or {}).get("id") or "")
            status = {
                "ok": True,
                "asset_id": out_asset_id,
                "version_id": out_version_id,
                "parent_version_id": parent_version_text,
                "token_source": token_source,
                "token_source_ref": token_source_ref,
                "message": "Branch version created.",
            }
            summary = f"SUCCESS: Branch version created ({out_version_id}) on asset {out_asset_id}."
            summary += key_loaded_note(token_source, token_source_ref)
            status_json = json.dumps(status, indent=2)
            _log_ok("CreateBranchVersionInGenAsset", asset_id=out_asset_id, version_id=out_version_id)
            return {"ui": {"text": [summary]}, "result": (image, out_asset_id, out_version_id, status_json)}
        except Exception as exc:
            _log_error("CreateBranchVersionInGenAsset", exc)
            status = {"ok": False, "error": str(exc)}
            summary = f"ERROR: {status['error']}"
            status_json = json.dumps(status, indent=2)
            return {"ui": {"text": [summary]}, "result": (image, "", "", status_json)}


class GenAssetLoadRecipeToWidgets:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "workflow_json": ("STRING", {"default": "{}", "multiline": True}),
                "metadata_json": ("STRING", {"default": "{}", "multiline": True}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "INT", "INT", "FLOAT", "STRING", "STRING", "FLOAT", "INT", "INT", "STRING")
    RETURN_NAMES = (
        "prompt_text",
        "negative_prompt_text",
        "model_name",
        "seed",
        "steps",
        "cfg",
        "sampler_name",
        "scheduler",
        "denoise",
        "width",
        "height",
        "status_json",
    )
    FUNCTION = "extract"
    CATEGORY = CATEGORY

    def extract(self, workflow_json: str, metadata_json: str):
        try:
            workflow = parse_json(workflow_json, {})
            metadata = parse_json(metadata_json, {})
            if not isinstance(workflow, dict):
                workflow = {}
            if not isinstance(metadata, dict):
                metadata = {}

            api_prompt = pick_dict(workflow, "api_prompt")
            pos_prompt, neg_prompt = extract_prompt_pair_from_api_prompt(api_prompt)

            performance = pick_dict(metadata, "performance")
            model_info = pick_dict(metadata, "model")
            image_info = pick_dict(metadata, "image")

            prompt_out = pick_string(pos_prompt, metadata.get("prompt"), metadata.get("positive_prompt"))
            negative_out = pick_string(neg_prompt, metadata.get("negative_prompt"))
            model_out = pick_string(model_info.get("checkpoint"), metadata.get("model"))
            seed_out = pick_int(performance.get("seed"), metadata.get("seed"))
            steps_out = pick_int(performance.get("steps"), metadata.get("steps"))
            cfg_out = pick_float(performance.get("cfg"), metadata.get("cfg"))
            sampler_out = pick_string(performance.get("sampler_name"), metadata.get("sampler_name"))
            scheduler_out = pick_string(performance.get("scheduler"), metadata.get("scheduler"))
            denoise_out = pick_float(performance.get("denoise"), metadata.get("denoise"))
            width_out = pick_int(image_info.get("width"), metadata.get("width"))
            height_out = pick_int(image_info.get("height"), metadata.get("height"))

            status = {
                "ok": True,
                "extracted": {
                    "prompt": bool(prompt_out),
                    "negative_prompt": bool(negative_out),
                    "model": bool(model_out),
                    "seed": bool(seed_out),
                    "steps": bool(steps_out),
                    "cfg": bool(cfg_out),
                    "sampler_name": bool(sampler_out),
                    "scheduler": bool(scheduler_out),
                    "denoise": bool(denoise_out),
                    "width": bool(width_out),
                    "height": bool(height_out),
                },
            }
            _log_ok(
                "LoadRecipeToWidgets",
                model_name=model_out,
                seed=int(seed_out),
                width=int(width_out),
                height=int(height_out),
            )
            return (
                prompt_out,
                negative_out,
                model_out,
                int(seed_out),
                int(steps_out),
                float(cfg_out),
                sampler_out,
                scheduler_out,
                float(denoise_out),
                int(width_out),
                int(height_out),
                json.dumps(status, indent=2),
            )
        except Exception as exc:
            _log_error("LoadRecipeToWidgets", exc)
            status = {"ok": False, "error": str(exc)}
            return ("", "", "", 0, 0, 0.0, "", "", 0.0, 0, 0, json.dumps(status, indent=2))


class GenAssetFindAssets:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "search_query": ("STRING", {"default": "", "tooltip": "Name/tags/prompt keyword search. Empty lists recent assets."}),
                "page": ("INT", {"default": 1, "min": 1, "max": 9999}),
                "page_size": ("INT", {"default": 20, "min": 1, "max": 64}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("assets_json", "asset_ids_csv", "status_json")
    FUNCTION = "search"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def search(self, base_url: str, token: str, search_query: str, page: int, page_size: int):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            query: list[tuple[str, str]] = [("page", str(int(max(1, page)))), ("page_size", str(int(max(1, min(64, page_size)))))]
            if search_query.strip():
                query.append(("search", search_query.strip()))
            url = urllib.parse.urljoin(clean_base_url + "/", "api/v1/assets")
            if query:
                url = f"{url}?{urllib.parse.urlencode(query)}"
            data = request_json("GET", url, clean_workspace_token)
            assets = data.get("assets") if isinstance(data.get("assets"), list) else []
            compact = [compact_asset_row(item) for item in assets if isinstance(item, dict)]
            asset_ids = [item["id"] for item in compact if item.get("id")]
            status = {
                "ok": True,
                "count": len(compact),
                "search_query": search_query.strip(),
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            return (json.dumps(compact, indent=2), ", ".join(asset_ids), json.dumps(status, indent=2))
        except Exception as exc:
            _log_error("FindAssetsInGenAsset", exc)
            return ("[]", "", json.dumps({"ok": False, "error": str(exc)}, indent=2))


class GenAssetListAssetVersions:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_id": ("STRING", {"default": "", "tooltip": "Required asset id (UUID)."}),
                "max_versions": ("INT", {"default": 20, "min": 1, "max": 200}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("versions_json", "version_ids_csv", "status_json")
    FUNCTION = "list_versions"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def list_versions(self, base_url: str, token: str, asset_id: str, max_versions: int):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            asset_id_text = asset_id.strip()
            if not asset_id_text:
                raise RuntimeError("Paste asset_id to list versions.")
            path = f"api/v1/assets/{urllib.parse.quote(asset_id_text)}"
            url = urllib.parse.urljoin(clean_base_url + "/", path)
            data = request_json("GET", url, clean_workspace_token)
            versions = data.get("versions") if isinstance(data.get("versions"), list) else []
            sorted_versions = sorted(
                [item for item in versions if isinstance(item, dict)],
                key=lambda item: int(item.get("version_number") or 0),
                reverse=True,
            )
            limited = sorted_versions[: int(max(1, max_versions))]
            compact = [compact_version_row(item) for item in limited]
            version_ids = [item["id"] for item in compact if item.get("id")]
            status = {
                "ok": True,
                "asset_id": asset_id_text,
                "count": len(compact),
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            return (json.dumps(compact, indent=2), ", ".join(version_ids), json.dumps(status, indent=2))
        except Exception as exc:
            _log_error("ListAssetVersionsInGenAsset", exc)
            return ("[]", "", json.dumps({"ok": False, "error": str(exc)}, indent=2))


class GenAssetLoadCurrentVersion:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_id": ("STRING", {"default": "", "tooltip": "Required asset id (UUID)."}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "asset_id", "version_id", "workflow_json", "metadata_json", "status_json")
    FUNCTION = "load_current"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def load_current(self, base_url: str, token: str, asset_id: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            asset_id_text = asset_id.strip()
            if not asset_id_text:
                raise RuntimeError("Paste asset_id to load current version.")
            path = f"api/v1/assets/{urllib.parse.quote(asset_id_text)}"
            url = urllib.parse.urljoin(clean_base_url + "/", path)
            data = request_json("GET", url, clean_workspace_token)
            asset = data.get("asset") if isinstance(data.get("asset"), dict) else {}
            versions = data.get("versions") if isinstance(data.get("versions"), list) else []
            current_version_id = str(asset.get("current_version_id") or "")
            if current_version_id:
                version = next((v for v in versions if isinstance(v, dict) and str(v.get("id") or "") == current_version_id), None)
            else:
                sorted_versions = sorted(
                    [item for item in versions if isinstance(item, dict)],
                    key=lambda item: int(item.get("version_number") or 0),
                    reverse=True,
                )
                version = sorted_versions[0] if sorted_versions else None
            if not isinstance(version, dict):
                raise RuntimeError("No versions found for this asset.")
            version_id = str(version.get("id") or "")
            if not version_id:
                raise RuntimeError("Matched version has no id.")
            payload = load_version_payload(clean_base_url, clean_workspace_token, version_id)
            loaded_version = payload.get("version") if isinstance(payload.get("version"), dict) else version
            preview_url = str(loaded_version.get("signed_preview_url") or "")
            if not preview_url:
                raise RuntimeError("Current version did not include a signed preview URL.")
            image = pil_to_tensor(download_image(preview_url))
            workflow_json = json.dumps(loaded_version.get("workflow_json") or {}, indent=2)
            metadata_json = json.dumps(loaded_version.get("metadata") or {}, indent=2)
            status = {
                "ok": True,
                "asset_id": asset_id_text,
                "version_id": version_id,
                "load_mode": "current_version",
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            return (image, asset_id_text, version_id, workflow_json, metadata_json, json.dumps(status, indent=2))
        except Exception as exc:
            _log_error("LoadCurrentVersionForAsset", exc)
            return (blank_image(), "", "", "{}", "{}", json.dumps({"ok": False, "error": str(exc)}, indent=2))


class GenAssetPromoteVersion:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_id": ("STRING", {"default": "", "tooltip": "Asset id (UUID)."}),
                "version_id": ("STRING", {"default": "", "tooltip": "Version id to set as current."}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("asset_id", "version_id", "status_json")
    FUNCTION = "promote"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def promote(self, base_url: str, token: str, asset_id: str, version_id: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            asset_id_text = asset_id.strip()
            version_id_text = version_id.strip()
            if not asset_id_text:
                raise RuntimeError("Paste asset_id.")
            if not version_id_text:
                raise RuntimeError("Paste version_id.")
            path = f"api/v1/assets/{urllib.parse.quote(asset_id_text)}"
            url = urllib.parse.urljoin(clean_base_url + "/", path)
            data = request_json("PATCH", url, clean_workspace_token, {"current_version_id": version_id_text})
            status = {
                "ok": True,
                "asset_id": asset_id_text,
                "version_id": version_id_text,
                "asset": compact_asset_row((data.get("asset") or {}) if isinstance(data.get("asset"), dict) else {}),
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            summary = f"SUCCESS: asset {asset_id_text} now points to version {version_id_text}."
            summary += key_loaded_note(token_source, token_source_ref)
            _log_ok("PromoteVersionInGenAsset", asset_id=asset_id_text, version_id=version_id_text)
            return {"ui": {"text": [summary]}, "result": (asset_id_text, version_id_text, json.dumps(status, indent=2))}
        except Exception as exc:
            _log_error("PromoteVersionInGenAsset", exc)
            status = {"ok": False, "error": str(exc)}
            summary = f"ERROR: {status['error']}"
            return {"ui": {"text": [summary]}, "result": ("", "", json.dumps(status, indent=2))}


class GenAssetDeleteVersion:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "version_id": ("STRING", {"default": "", "tooltip": "Version id to delete."}),
                "confirm_delete": ("BOOLEAN", {"default": False}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("version_id", "status_json")
    FUNCTION = "delete"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def delete(self, base_url: str, token: str, version_id: str, confirm_delete: bool):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, _, _ = resolve_workspace_token(token)
            version_id_text = version_id.strip()
            if not version_id_text:
                raise RuntimeError("Paste version_id.")
            if not confirm_delete:
                raise RuntimeError("Set confirm_delete=true to delete a version.")
            path = f"api/v1/versions/{urllib.parse.quote(version_id_text)}"
            url = urllib.parse.urljoin(clean_base_url + "/", path)
            data = request_json("DELETE", url, clean_workspace_token)
            _log_ok("DeleteVersionInGenAsset", version_id=version_id_text)
            return {"ui": {"text": [f"SUCCESS: deleted version {version_id_text}."]}, "result": (version_id_text, json.dumps({"ok": True, "result": data}, indent=2))}
        except Exception as exc:
            _log_error("DeleteVersionInGenAsset", exc)
            status = {"ok": False, "error": str(exc)}
            return {"ui": {"text": [f"ERROR: {status['error']}"]}, "result": ("", json.dumps(status, indent=2))}


class GenAssetForkAssetFromVersion:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "source_version_id": ("STRING", {"default": "", "tooltip": "Existing version id to fork from."}),
                "new_asset_name": ("STRING", {"default": "Forked Asset"}),
                "prompt_suffix": ("STRING", {"default": "forked in comfyui", "multiline": True}),
                "negative_prompt_override": ("STRING", {"default": "", "multiline": True}),
                "tags_csv": ("STRING", {"default": "forked"}),
                "intent": ("STRING", {"default": "fork"}),
                "extra_metadata_json": ("STRING", {"default": "{\n  \"fork\": true\n}", "multiline": True}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "asset_id", "version_id", "status_json")
    FUNCTION = "fork"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def fork(
        self,
        base_url: str,
        token: str,
        source_version_id: str,
        new_asset_name: str,
        prompt_suffix: str,
        negative_prompt_override: str,
        tags_csv: str,
        intent: str,
        extra_metadata_json: str,
    ):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, _ = resolve_workspace_token(token)
            source_version_text = source_version_id.strip()
            if not source_version_text:
                raise RuntimeError("Paste source_version_id.")
            if not new_asset_name.strip():
                raise RuntimeError("Set new_asset_name.")
            extra_meta = parse_json(extra_metadata_json, {})
            if not isinstance(extra_meta, dict):
                raise RuntimeError("extra_metadata_json must be a JSON object.")
            source_payload = load_version_payload(clean_base_url, clean_workspace_token, source_version_text)
            source_version = source_payload.get("version") if isinstance(source_payload.get("version"), dict) else {}
            preview_url = str(source_version.get("signed_preview_url") or "")
            if not preview_url:
                raise RuntimeError("Source version missing signed_preview_url.")
            image_pil = download_image(preview_url)
            image = pil_to_tensor(image_pil)
            prompt_text = str(source_version.get("prompt") or "").strip()
            suffix = prompt_suffix.strip()
            if suffix:
                prompt_text = f"{prompt_text}, {suffix}" if prompt_text else suffix
            negative_prompt = negative_prompt_override.strip() or str(source_version.get("negative_prompt") or "")
            model_name = str(source_version.get("model") or "")
            seed_value = int(source_version.get("seed") or 0)
            merged_metadata = {
                **extra_meta,
                "fork": {
                    "from_version_id": source_version_text,
                    "from_asset_id": str(source_version.get("asset_id") or ""),
                    "created_in": "comfyui",
                },
            }
            fields = {
                "asset_name": new_asset_name.strip(),
                "asset_id": "",
                "prompt": prompt_text,
                "negative_prompt": negative_prompt,
                "workflow_json": json.dumps(source_version.get("workflow_json") or {}),
                "model": model_name,
                "seed": str(seed_value),
                "tags": tags_csv,
                "intent": intent.strip() or "fork",
                "metadata": json.dumps(merged_metadata),
                "source": "comfyui-fork",
            }
            data = post_multipart(
                urllib.parse.urljoin(clean_base_url + "/", "api/v1/generations"),
                clean_workspace_token,
                fields,
                {"image": ("forked_version.png", tensor_to_png_bytes(image), "image/png")},
            )
            out_asset_id = str((data.get("asset") or {}).get("id") or "")
            out_version_id = str((data.get("version") or {}).get("id") or "")
            _log_ok("ForkAssetFromVersionInGenAsset", asset_id=out_asset_id, version_id=out_version_id, token_source=token_source)
            return (image, out_asset_id, out_version_id, json.dumps({"ok": True, "source_version_id": source_version_text, "asset_id": out_asset_id, "version_id": out_version_id}, indent=2))
        except Exception as exc:
            _log_error("ForkAssetFromVersionInGenAsset", exc)
            return (blank_image(), "", "", json.dumps({"ok": False, "error": str(exc)}, indent=2))


class GenAssetCreateAsset:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_name": ("STRING", {"default": "New Asset"}),
                "prompt_text": ("STRING", {"default": "", "multiline": True}),
                "negative_prompt_text": ("STRING", {"default": "", "multiline": True}),
                "model_name": ("STRING", {"default": ""}),
                "seed": ("INT", {"default": 0, "min": 0}),
                "tags_csv": ("STRING", {"default": ""}),
                "intent": ("STRING", {"default": "create_asset"}),
                "extra_metadata_json": ("STRING", {"default": "{}", "multiline": True}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "asset_id", "version_id", "status_json")
    FUNCTION = "create_asset"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def create_asset(
        self,
        image: torch.Tensor,
        base_url: str,
        token: str,
        asset_name: str,
        prompt_text: str,
        negative_prompt_text: str,
        model_name: str,
        seed: int,
        tags_csv: str,
        intent: str,
        extra_metadata_json: str,
    ):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, _ = resolve_workspace_token(token)
            asset_name_text = asset_name.strip()
            if not asset_name_text:
                raise RuntimeError("Set asset_name.")
            metadata = parse_json(extra_metadata_json, {})
            if not isinstance(metadata, dict):
                raise RuntimeError("extra_metadata_json must be a JSON object.")
            fields = {
                "asset_name": asset_name_text,
                "asset_id": "",
                "prompt": prompt_text,
                "negative_prompt": negative_prompt_text,
                "workflow_json": "{}",
                "model": model_name,
                "seed": str(int(seed)),
                "tags": tags_csv,
                "intent": intent.strip() or "create_asset",
                "metadata": json.dumps(metadata),
                "source": "comfyui-create",
            }
            data = post_multipart(
                urllib.parse.urljoin(clean_base_url + "/", "api/v1/generations"),
                clean_workspace_token,
                fields,
                {"image": ("create_asset.png", tensor_to_png_bytes(image), "image/png")},
            )
            out_asset_id = str((data.get("asset") or {}).get("id") or "")
            out_version_id = str((data.get("version") or {}).get("id") or "")
            _log_ok("CreateAssetInGenAsset", asset_id=out_asset_id, version_id=out_version_id, token_source=token_source)
            return (image, out_asset_id, out_version_id, json.dumps({"ok": True, "asset_id": out_asset_id, "version_id": out_version_id}, indent=2))
        except Exception as exc:
            _log_error("CreateAssetInGenAsset", exc)
            return (image, "", "", json.dumps({"ok": False, "error": str(exc)}, indent=2))


class GenAssetRenameAsset:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_id": ("STRING", {"default": "", "tooltip": "Asset id to rename."}),
                "new_name": ("STRING", {"default": ""}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("asset_id", "asset_name", "status_json")
    FUNCTION = "rename"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def rename(self, base_url: str, token: str, asset_id: str, new_name: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            asset_id_text = asset_id.strip()
            name_text = new_name.strip()
            if not asset_id_text:
                raise RuntimeError("Paste asset_id.")
            if not name_text:
                raise RuntimeError("Set new_name.")
            path = f"api/v1/assets/{urllib.parse.quote(asset_id_text)}"
            data = request_json("PATCH", urllib.parse.urljoin(clean_base_url + "/", path), clean_workspace_token, {"name": name_text})
            asset = (data.get("asset") or {}) if isinstance(data.get("asset"), dict) else {}
            out_name = str(asset.get("name") or name_text)
            status = {"ok": True, "asset_id": asset_id_text, "asset_name": out_name, "token_source": token_source, "token_source_ref": token_source_ref}
            _log_ok("RenameAssetInGenAsset", asset_id=asset_id_text)
            return {"ui": {"text": [f"SUCCESS: renamed asset to '{out_name}'." ]}, "result": (asset_id_text, out_name, json.dumps(status, indent=2))}
        except Exception as exc:
            _log_error("RenameAssetInGenAsset", exc)
            status = {"ok": False, "error": str(exc)}
            return {"ui": {"text": [f"ERROR: {status['error']}"]}, "result": ("", "", json.dumps(status, indent=2))}


class GenAssetUpsertAssetFields:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_id": ("STRING", {"default": "", "tooltip": "Asset id to patch."}),
                "tags_csv": ("STRING", {"default": ""}),
                "notes_md": ("STRING", {"default": "", "multiline": True}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("asset_id", "asset_json", "status_json")
    FUNCTION = "upsert"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def upsert(self, base_url: str, token: str, asset_id: str, tags_csv: str, notes_md: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, token_source, token_source_ref = resolve_workspace_token(token)
            asset_id_text = asset_id.strip()
            if not asset_id_text:
                raise RuntimeError("Paste asset_id.")
            tags = parse_tags_csv(tags_csv)
            path = f"api/v1/assets/{urllib.parse.quote(asset_id_text)}"
            data = request_json(
                "PATCH",
                urllib.parse.urljoin(clean_base_url + "/", path),
                clean_workspace_token,
                {"tags": tags, "notes_md": notes_md},
            )
            asset = (data.get("asset") or {}) if isinstance(data.get("asset"), dict) else {}
            compact = compact_asset_row(asset)
            compact["notes_md"] = str(asset.get("notes_md") or "")
            status = {
                "ok": True,
                "asset_id": asset_id_text,
                "updated_tags_count": len(tags),
                "updated_notes_len": len(notes_md or ""),
                "token_source": token_source,
                "token_source_ref": token_source_ref,
            }
            return (asset_id_text, json.dumps(compact, indent=2), json.dumps(status, indent=2))
        except Exception as exc:
            _log_error("UpsertAssetTagsFields", exc)
            return ("", "{}", json.dumps({"ok": False, "error": str(exc)}, indent=2))


class GenAssetAssetSummary:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "token": ("STRING", {"default": TOKEN_FILE_HINT, "multiline": False}),
                "asset_id": ("STRING", {"default": "", "tooltip": "Asset id to summarize."}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("summary", "asset_json", "status_json")
    FUNCTION = "summary"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def summary(self, base_url: str, token: str, asset_id: str):
        try:
            clean_base_url = require_base_url(base_url)
            clean_workspace_token, _, _ = resolve_workspace_token(token)
            asset_id_text = asset_id.strip()
            if not asset_id_text:
                raise RuntimeError("Paste asset_id.")
            path = f"api/v1/assets/{urllib.parse.quote(asset_id_text)}"
            data = request_json("GET", urllib.parse.urljoin(clean_base_url + "/", path), clean_workspace_token)
            asset = (data.get("asset") or {}) if isinstance(data.get("asset"), dict) else {}
            versions = data.get("versions") if isinstance(data.get("versions"), list) else []
            current_version_id = str(asset.get("current_version_id") or "")
            current = next((v for v in versions if isinstance(v, dict) and str(v.get("id") or "") == current_version_id), None)
            if current is None and versions:
                current = versions[0] if isinstance(versions[0], dict) else None
            current_version_number = int(current.get("version_number") or 0) if isinstance(current, dict) else 0
            compact = compact_asset_row(asset)
            compact["version_count"] = len([v for v in versions if isinstance(v, dict)])
            compact["current_version_number"] = current_version_number
            summary = (
                f"asset={compact.get('name') or asset_id_text} | versions={compact['version_count']} | "
                f"current=v{current_version_number} ({current_version_id or 'n/a'})"
            )
            status = {"ok": True, "asset_id": asset_id_text, "version_count": compact["version_count"], "current_version_id": current_version_id}
            return (summary, json.dumps(compact, indent=2), json.dumps(status, indent=2))
        except Exception as exc:
            _log_error("AssetSummaryInGenAsset", exc)
            return ("", "{}", json.dumps({"ok": False, "error": str(exc)}, indent=2))
