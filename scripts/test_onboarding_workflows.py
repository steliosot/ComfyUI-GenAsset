from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
import tempfile
import types
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL_FILE_RE = re.compile(r"\.(safetensors|ckpt|pt|pth|bin|gguf|onnx)$", re.I)
SECRET_FIXTURE = "sk-test-super-secret-value-1234567890"

FOLDER_BY_KEY = {
    "ckpt_name": "checkpoints",
    "checkpoint": "checkpoints",
    "unet_name": "unet",
    "diffusion_model_name": "diffusion_models",
    "vae_name": "vae",
    "lora_name": "loras",
    "control_net_name": "controlnet",
    "controlnet_name": "controlnet",
    "clip_name": "clip",
    "clip_name1": "clip",
    "clip_name2": "clip",
    "clip_name3": "clip",
    "t5_name": "clip",
    "clip_l_name": "clip",
    "clip_g_name": "clip",
    "clip_vision_name": "clip_vision",
    "style_model_name": "style_models",
    "upscale_model_name": "upscale_models",
    "embedding_name": "embeddings",
    "ipadapter_file": "ipadapter",
    "ipadapter_name": "ipadapter",
}


def load_health_module():
    package = types.ModuleType("ComfyUI_GenAsset")
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location("ComfyUI_GenAsset.health", ROOT / "health.py")
    if spec is None or spec.loader is None:
        raise SystemExit("Could not load health module.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def gui_workflow_to_api_prompt(workflow: dict[str, Any]) -> dict[str, Any]:
    link_map: dict[Any, list[Any]] = {}
    for link in workflow.get("links") or []:
        if isinstance(link, list) and len(link) >= 5:
            link_map[link[0]] = [str(link[1]), int(link[2] or 0)]

    prompt: dict[str, Any] = {}
    for node in workflow.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        inputs: dict[str, Any] = {}
        widgets = list(node.get("widgets_values") or [])
        widget_index = 0
        for item in node.get("inputs") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("localized_name") or "")
            if not name:
                continue
            link_id = item.get("link")
            if link_id is not None and link_id in link_map:
                inputs[name] = link_map[link_id]
            elif isinstance(item.get("widget"), dict):
                if widget_index < len(widgets):
                    inputs[name] = widgets[widget_index]
                widget_index += 1
        prompt[str(node.get("id"))] = {"class_type": str(node.get("type") or ""), "inputs": inputs}
    return prompt


def install_folder_paths_stub(model_root: Path) -> None:
    folder_paths = types.ModuleType("folder_paths")
    folders = [
        "checkpoints",
        "unet",
        "diffusion_models",
        "unet_gguf",
        "loras",
        "vae",
        "controlnet",
        "controlnet_gguf",
        "clip",
        "clip_vision",
        "embeddings",
        "upscale_models",
        "style_models",
        "ipadapter",
    ]
    folder_paths.folder_names_and_paths = {folder: ([str(model_root / folder)], set()) for folder in folders}
    folder_paths.folder_names_and_paths["custom_nodes"] = ([str(ROOT.parent)], set())

    def get_full_path(folder: str, name: str):
        roots = folder_paths.folder_names_and_paths.get(folder, ([], set()))[0]
        for root in roots:
            candidate = Path(root) / name
            if candidate.is_file():
                return str(candidate)
        return None

    folder_paths.get_full_path = get_full_path
    sys.modules["folder_paths"] = folder_paths


def folder_for_model_ref(node_class: str, input_key: str) -> str:
    key = input_key.lower()
    klass = node_class.lower()
    if key in FOLDER_BY_KEY:
        return FOLDER_BY_KEY[key]
    if "lora" in klass:
        return "loras"
    if "checkpoint" in klass or "ckpt" in klass:
        return "checkpoints"
    if "unet" in klass or "gguf" in klass or "diffusion" in klass:
        return "unet"
    if "vae" in klass:
        return "vae"
    if "controlnet" in klass or "control_net" in klass:
        return "controlnet"
    if "clip" in klass:
        return "clip"
    if "upscale" in klass:
        return "upscale_models"
    if "embedding" in klass:
        return "embeddings"
    return "checkpoints"


def seed_model_placeholders(model_root: Path, prompts: list[tuple[Path, dict[str, Any]]]) -> None:
    for path, prompt in prompts:
        for node in prompt.values():
            if not isinstance(node, dict):
                continue
            node_class = str(node.get("class_type") or "")
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            for key, value in inputs.items():
                if isinstance(value, str) and MODEL_FILE_RE.search(value) and not value.startswith(("http://", "https://")):
                    target = model_root / folder_for_model_ref(node_class, str(key)) / value
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f"{path.name}:{value}".encode("utf-8"))


def main() -> None:
    health = load_health_module()
    workflow_paths = sorted((ROOT / "workflows").glob("*.json"))
    if not workflow_paths:
        raise AssertionError("Expected bundled onboarding workflows.")

    prompts: list[tuple[Path, dict[str, Any]]] = []
    known_node_types: set[str] = set()
    for path in workflow_paths:
        prompt = gui_workflow_to_api_prompt(json.loads(path.read_text(encoding="utf-8")))
        prompts.append((path, prompt))
        known_node_types.update(str(node.get("class_type") or "") for node in prompt.values() if isinstance(node, dict))

    with tempfile.TemporaryDirectory() as temp_dir:
        model_root = Path(temp_dir)
        install_folder_paths_stub(model_root)
        seed_model_placeholders(model_root, prompts)

        for path, prompt in prompts:
            workflow = json.loads(path.read_text(encoding="utf-8"))
            diagnostics = health.diagnose_workflow(prompt, known_node_types=sorted(known_node_types))
            resolved = health.resolve_models(prompt, include_hashes=True)
            repro = health.build_repro_lock(prompt, workflow)
            assert diagnostics["summary"]["node_count"] > 0, path.name
            assert not any(issue["kind"] == "missing_custom_node" for issue in diagnostics["issues"]), (path.name, diagnostics["issues"])
            assert not any(issue["kind"] == "broken_link" for issue in diagnostics["issues"]), (path.name, diagnostics["issues"])
            assert resolved["summary"]["missing"] == 0, (path.name, resolved)
            assert repro["schema"] == "genasset.repro_lock.v1", path.name
            assert repro["workflow_hash"], path.name

        base_prompt = copy.deepcopy(prompts[0][1])

        missing_model = copy.deepcopy(base_prompt)
        missing_model["999"] = {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "definitely-missing-onboarding-model.safetensors"},
        }
        missing_model_diag = health.diagnose_workflow(missing_model, known_node_types=sorted(known_node_types) + ["CheckpointLoaderSimple"])
        assert any(issue["kind"] == "missing_model" for issue in missing_model_diag["issues"]), missing_model_diag

        missing_node = copy.deepcopy(base_prompt)
        missing_node["777"] = {"class_type": "GenAssetTotallyMissingNode", "inputs": {}}
        missing_node_diag = health.diagnose_workflow(missing_node, known_node_types=sorted(known_node_types))
        assert any(issue["kind"] == "missing_custom_node" for issue in missing_node_diag["issues"]), missing_node_diag

        broken_link = copy.deepcopy(base_prompt)
        first_node_id = next(iter(broken_link))
        broken_link[first_node_id]["inputs"]["image"] = ["999999", 0]
        broken_link_diag = health.diagnose_workflow(broken_link, known_node_types=sorted(known_node_types))
        assert any(issue["kind"] == "broken_link" for issue in broken_link_diag["issues"]), broken_link_diag

        prompt_text = {
            "1": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "words flux1-dev.safetensors on a poster, not a model field"},
            }
        }
        false_positive = health.resolve_models(prompt_text)
        assert false_positive["summary"]["total"] == 0, false_positive

        heavy = copy.deepcopy(base_prompt)
        heavy["888"] = {"class_type": "WanVideoSampler", "inputs": {}}
        heavy_diag = health.diagnose_workflow(heavy, known_node_types=sorted(known_node_types) + ["WanVideoSampler"])
        assert any(issue["kind"] == "video_or_heavy_workflow" for issue in heavy_diag["issues"]), heavy_diag

        secret_payload = health.build_health_payload(
            {
                "prompt": {"1": {"class_type": "GenAssetSaveGeneration", "inputs": {"token": SECRET_FIXTURE}}},
                "workflow": {"nodes": [{"api_key": SECRET_FIXTURE}]},
                "known_node_types": ["GenAssetSaveGeneration"],
            }
        )
        secret_text = json.dumps(secret_payload)
        assert SECRET_FIXTURE not in secret_text, secret_text
        assert "[redacted]" in secret_text, secret_text

    print(f"GenAsset onboarding workflow tests passed ({len(workflow_paths)} workflows).")


if __name__ == "__main__":
    main()
