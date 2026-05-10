from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
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


def request_json(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    return read_json(request)


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
