from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any

try:
    import torch
except Exception:  # pragma: no cover - ComfyUI normally provides torch.
    torch = None  # type: ignore


SECRET_KEYS = {"workspace_token", "token", "authorization", "api_key", "secret", "service_role_key"}
IGNORED_MODEL_VALUES = {"", "none", "null", "undefined", "default", "auto", "random"}
MODEL_FILE_RE = re.compile(r"\.(safetensors|ckpt|pt|pth|bin|gguf|onnx|diffusers)$", re.I)

MODEL_KEY_FOLDERS: dict[str, list[str]] = {
    "ckpt_name": ["checkpoints"],
    "checkpoint": ["checkpoints"],
    "unet_name": ["unet", "diffusion_models", "unet_gguf"],
    "diffusion_model_name": ["diffusion_models", "unet"],
    "vae_name": ["vae"],
    "lora_name": ["loras"],
    "control_net_name": ["controlnet", "controlnet_gguf"],
    "controlnet_name": ["controlnet", "controlnet_gguf"],
    "clip_name": ["clip"],
    "clip_name1": ["clip"],
    "clip_name2": ["clip"],
    "clip_name3": ["clip"],
    "t5_name": ["clip"],
    "clip_l_name": ["clip"],
    "clip_g_name": ["clip"],
    "clip_vision_name": ["clip_vision"],
    "style_model_name": ["style_models"],
    "upscale_model_name": ["upscale_models"],
    "embedding_name": ["embeddings"],
    "ipadapter_file": ["ipadapter"],
    "ipadapter_name": ["ipadapter"],
}


def redact_secret_fields(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                out[key] = "[redacted]"
            else:
                out[key] = redact_secret_fields(item)
        return out
    if isinstance(value, list):
        return [redact_secret_fields(item) for item in value]
    return value


def stable_json_hash(value: Any) -> str:
    normalized = json.dumps(redact_secret_fields(value), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def api_prompt_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt")
    if isinstance(prompt, dict) and isinstance(prompt.get("output"), dict):
        prompt = prompt.get("output")
    if isinstance(prompt, dict):
        return prompt
    return {}


def workflow_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    workflow = payload.get("workflow")
    if isinstance(workflow, dict):
        return workflow
    prompt = payload.get("prompt")
    if isinstance(prompt, dict) and isinstance(prompt.get("workflow"), dict):
        return prompt.get("workflow") or {}
    return {}


def _input_node_id(value: Any) -> str | None:
    if isinstance(value, (list, tuple)) and value:
        return str(value[0])
    return None


def _node_class(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("class_type") or node.get("type") or "")


def _node_inputs(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _looks_like_model_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.lower() in IGNORED_MODEL_VALUES:
        return False
    if text.startswith(("http://", "https://")):
        return False
    if len(text) > 260:
        return False
    return bool(MODEL_FILE_RE.search(text))


def _looks_like_model_key(input_key: str) -> bool:
    key = input_key.lower()
    if key in MODEL_KEY_FOLDERS:
        return True
    model_terms = (
        "model",
        "ckpt",
        "checkpoint",
        "unet",
        "vae",
        "lora",
        "controlnet",
        "control_net",
        "clip",
        "embedding",
        "upscale",
        "ipadapter",
        "style",
    )
    value_terms = ("name", "file", "path")
    return any(term in key for term in model_terms) and any(term in key for term in value_terms)


def _folders_for_model_ref(node_class: str, input_key: str, value: Any) -> tuple[str, list[str]]:
    key = input_key.lower()
    klass = node_class.lower()
    if key in MODEL_KEY_FOLDERS:
        return _type_from_folder(MODEL_KEY_FOLDERS[key][0]), MODEL_KEY_FOLDERS[key]
    if not (_looks_like_model_ref(value) or _looks_like_model_key(key)):
        return "", []
    if "lora" in klass:
        return "lora", ["loras"]
    if "checkpoint" in klass or "ckpt" in klass:
        return "checkpoint", ["checkpoints"]
    if "unet" in klass or "gguf" in klass or "diffusion" in klass:
        return "diffusion_model", ["unet", "diffusion_models", "unet_gguf"]
    if "vae" in klass:
        return "vae", ["vae"]
    if "controlnet" in klass or "control_net" in klass:
        return "controlnet", ["controlnet", "controlnet_gguf"]
    if "clipvision" in klass or "clip_vision" in klass:
        return "clip_vision", ["clip_vision"]
    if "clip" in klass:
        return "clip", ["clip"]
    if "upscale" in klass:
        return "upscale_model", ["upscale_models"]
    if "embedding" in klass:
        return "embedding", ["embeddings"]
    if _looks_like_model_ref(value):
        return "model", ["checkpoints", "diffusion_models", "unet", "loras", "vae", "controlnet", "clip", "upscale_models"]
    return "", []


def _type_from_folder(folder: str) -> str:
    return {
        "checkpoints": "checkpoint",
        "diffusion_models": "diffusion_model",
        "unet": "diffusion_model",
        "unet_gguf": "diffusion_model",
        "loras": "lora",
        "vae": "vae",
        "controlnet": "controlnet",
        "controlnet_gguf": "controlnet",
        "clip": "clip",
        "clip_vision": "clip_vision",
        "embeddings": "embedding",
        "upscale_models": "upscale_model",
        "style_models": "style_model",
        "ipadapter": "ipadapter",
    }.get(folder, "model")


def _folder_label(folder: str) -> str:
    labels = {
        "checkpoints": "models/checkpoints",
        "diffusion_models": "models/diffusion_models",
        "unet": "models/unet",
        "unet_gguf": "models/diffusion_models or models/unet",
        "loras": "models/loras",
        "vae": "models/vae",
        "controlnet": "models/controlnet",
        "controlnet_gguf": "models/controlnet",
        "clip": "models/clip",
        "clip_vision": "models/clip_vision",
        "embeddings": "models/embeddings",
        "upscale_models": "models/upscale_models",
        "style_models": "models/style_models",
        "ipadapter": "models/ipadapter",
    }
    return labels.get(folder, f"models/{folder}")


def _folder_paths_module() -> Any:
    try:
        import folder_paths  # type: ignore

        return folder_paths
    except Exception:
        return None


def _find_model_path(name: str, folders: list[str]) -> tuple[Path | None, str]:
    folder_paths = _folder_paths_module()
    if folder_paths:
        for folder in folders:
            try:
                path = folder_paths.get_full_path(folder, name)
            except Exception:
                path = None
            if path and Path(path).is_file():
                return Path(path), folder
            try:
                roots = folder_paths.folder_names_and_paths.get(folder, ([], set()))[0]
            except Exception:
                roots = []
            for root in roots or []:
                candidate = Path(root) / name
                if candidate.is_file():
                    return candidate, folder
    models_dir = Path.cwd() / "models"
    for folder in folders:
        candidate = models_dir / folder / name
        if candidate.is_file():
            return candidate, folder
    return None, folders[0] if folders else ""


def _file_metadata(path: Path, include_hash: bool = False) -> dict[str, Any]:
    try:
        stat = path.stat()
    except Exception:
        return {}
    data: dict[str, Any] = {
        "path": str(path),
        "size_bytes": int(stat.st_size),
        "mtime": int(stat.st_mtime),
    }
    if include_hash:
        data["sha256"] = _hash_file(path)
    elif stat.st_size <= 256 * 1024 * 1024:
        data["sha256"] = _hash_file(path)
    else:
        data["sha256"] = ""
        data["hash_note"] = "Skipped automatically because file is larger than 256MB."
    return data


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_suggestions(name: str, model_type: str) -> list[dict[str, str]]:
    query = re.sub(r"\.(safetensors|ckpt|pt|pth|bin|gguf|onnx)$", "", name, flags=re.I).replace("\\", "/").split("/")[-1]
    encoded = query.replace(" ", "+")
    return [
        {"label": "Hugging Face search", "url": f"https://huggingface.co/models?search={encoded}", "type": model_type},
        {"label": "Civitai search", "url": f"https://civitai.com/search/models?query={encoded}", "type": model_type},
    ]


def resolve_models(api_prompt: dict[str, Any], include_hashes: bool = False) -> dict[str, Any]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for node_id, node in api_prompt.items():
        if not isinstance(node, dict):
            continue
        klass = _node_class(node)
        for key, value in _node_inputs(node).items():
            if isinstance(value, (list, tuple)):
                continue
            if not isinstance(value, str) or value.strip().lower() in IGNORED_MODEL_VALUES:
                continue
            model_type, folders = _folders_for_model_ref(klass, key, value)
            if not folders:
                continue
            name = value.strip()
            dedupe_key = (model_type, name.lower(), ",".join(folders))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            path, matched_folder = _find_model_path(name, folders)
            refs.append(
                {
                    "node_id": str(node_id),
                    "node_class": klass,
                    "input": key,
                    "name": name,
                    "type": model_type,
                    "status": "found" if path else "missing",
                    "expected_folders": [_folder_label(folder) for folder in folders],
                    "matched_folder": _folder_label(matched_folder) if path else "",
                    "file": _file_metadata(path, include_hash=include_hashes) if path else {},
                    "suggestions": [] if path else _source_suggestions(name, model_type),
                }
            )
    found = sum(1 for ref in refs if ref["status"] == "found")
    missing = sum(1 for ref in refs if ref["status"] == "missing")
    return {
        "ok": missing == 0,
        "models": refs,
        "summary": {
            "total": len(refs),
            "found": found,
            "missing": missing,
        },
    }


def diagnose_workflow(api_prompt: dict[str, Any], known_node_types: list[str] | None = None) -> dict[str, Any]:
    known = {str(item) for item in known_node_types or [] if str(item)}
    issues: list[dict[str, Any]] = []
    node_count = 0
    for node_id, node in api_prompt.items():
        if not isinstance(node, dict):
            continue
        node_count += 1
        klass = _node_class(node)
        if known and klass and klass not in known:
            issues.append(
                {
                    "severity": "error",
                    "kind": "missing_custom_node",
                    "node_id": str(node_id),
                    "node_class": klass,
                    "message": f"Node type {klass} is not registered in this ComfyUI session.",
                }
            )
        for input_name, value in _node_inputs(node).items():
            upstream_id = _input_node_id(value)
            if upstream_id and upstream_id not in api_prompt:
                issues.append(
                    {
                        "severity": "error",
                        "kind": "broken_link",
                        "node_id": str(node_id),
                        "input": input_name,
                        "missing_node_id": upstream_id,
                        "message": f"Input {input_name} links to missing node {upstream_id}.",
                    }
                )
        lower = klass.lower()
        if "wan" in lower or "video" in lower:
            issues.append(
                {
                    "severity": "warning",
                    "kind": "video_or_heavy_workflow",
                    "node_id": str(node_id),
                    "node_class": klass,
                    "message": "This workflow appears to use video/heavy nodes; confirm the target ComfyUI server supports them.",
                }
            )
    models = resolve_models(api_prompt)
    for ref in models["models"]:
        if ref["status"] == "missing":
            issues.append(
                {
                    "severity": "error",
                    "kind": "missing_model",
                    "node_id": ref["node_id"],
                    "node_class": ref["node_class"],
                    "model": ref["name"],
                    "model_type": ref["type"],
                    "expected_folders": ref["expected_folders"],
                    "message": f"Missing {ref['type']} model: {ref['name']}.",
                }
            )
    error_count = sum(1 for issue in issues if issue["severity"] == "error")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    return {
        "ok": error_count == 0,
        "summary": {
            "node_count": node_count,
            "error_count": error_count,
            "warning_count": warning_count,
            "model_count": models["summary"]["total"],
            "missing_model_count": models["summary"]["missing"],
        },
        "issues": issues,
        "models": models["models"],
    }


def environment_snapshot() -> dict[str, Any]:
    cuda_available = False
    cuda_version = ""
    mps_available = False
    torch_version = ""
    if torch is not None:
        torch_version = str(getattr(torch, "__version__", ""))
        cuda = getattr(torch, "cuda", None)
        if cuda is not None:
            try:
                cuda_available = bool(cuda.is_available())
                cuda_version = str(getattr(torch.version, "cuda", "") or "")
            except Exception:
                pass
        try:
            mps_available = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        except Exception:
            mps_available = False
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "torch": torch_version,
        "cuda": {"available": cuda_available, "version": cuda_version},
        "mps": {"available": mps_available},
        "comfyui": _comfyui_version(),
        "genasset_node": _genasset_version(),
        "custom_nodes": _custom_node_versions(),
    }


def _genasset_version() -> str:
    pyproject = Path(__file__).resolve().parent / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except Exception:
        return ""
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""


def _comfyui_version() -> dict[str, str]:
    cwd = Path.cwd()
    for base in (cwd, Path(__file__).resolve().parents[2]):
        git = base / ".git"
        if not git.exists():
            continue
        try:
            import subprocess

            commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=base, text=True, stderr=subprocess.DEVNULL).strip()
            branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=base, text=True, stderr=subprocess.DEVNULL).strip()
            return {"path": str(base), "branch": branch, "commit": commit}
        except Exception:
            return {"path": str(base), "branch": "", "commit": ""}
    return {}


def _custom_node_versions() -> list[dict[str, str]]:
    folder_paths = _folder_paths_module()
    roots: list[Path] = []
    if folder_paths:
        try:
            roots = [Path(item) for item in folder_paths.folder_names_and_paths.get("custom_nodes", ([], set()))[0]]
        except Exception:
            roots = []
    if not roots:
        roots = [Path(__file__).resolve().parents[1]]
    out: list[dict[str, str]] = []
    for root in roots[:4]:
        if not root.exists():
            continue
        for child in sorted(root.iterdir())[:200]:
            if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
                continue
            record = {"name": child.name, "version": "", "commit": ""}
            pyproject = child / "pyproject.toml"
            if pyproject.is_file():
                try:
                    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"))
                    if match:
                        record["version"] = match.group(1)
                except Exception:
                    pass
            if (child / ".git").exists():
                try:
                    import subprocess

                    record["commit"] = subprocess.check_output(
                        ["git", "rev-parse", "--short", "HEAD"],
                        cwd=child,
                        text=True,
                        stderr=subprocess.DEVNULL,
                    ).strip()
                except Exception:
                    pass
            out.append(record)
    return out


def build_repro_lock(api_prompt: dict[str, Any], workflow: dict[str, Any] | None = None) -> dict[str, Any]:
    workflow_payload = {"api_prompt": redact_secret_fields(api_prompt), "workflow": redact_secret_fields(workflow or {})}
    models = resolve_models(api_prompt, include_hashes=True)
    return {
        "schema": "genasset.repro_lock.v1",
        "workflow_hash": stable_json_hash(workflow_payload),
        "environment": environment_snapshot(),
        "models": models["models"],
        "model_summary": models["summary"],
    }


def build_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    api_prompt = api_prompt_from_payload(payload)
    workflow = workflow_from_payload(payload)
    known_node_types = payload.get("known_node_types")
    if not isinstance(known_node_types, list):
        known_node_types = []
    diagnostics = diagnose_workflow(api_prompt, known_node_types=[str(item) for item in known_node_types])
    repro = build_repro_lock(api_prompt, workflow)
    return {
        "source": "comfyui-genasset-health",
        "diagnostics": diagnostics,
        "repro_lock": repro,
        "workflow_json": redact_secret_fields({"api_prompt": api_prompt, "workflow": workflow}),
    }
