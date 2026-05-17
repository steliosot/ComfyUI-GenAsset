from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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


def install_folder_paths_stub(model_root: Path):
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.folder_names_and_paths = {
        "checkpoints": ([str(model_root / "checkpoints")], {".safetensors", ".ckpt"}),
        "unet": ([str(model_root / "unet")], {".gguf", ".safetensors"}),
        "diffusion_models": ([str(model_root / "diffusion_models")], {".gguf", ".safetensors"}),
        "loras": ([str(model_root / "loras")], {".safetensors"}),
        "vae": ([str(model_root / "vae")], {".safetensors"}),
        "controlnet": ([str(model_root / "controlnet")], {".safetensors"}),
        "clip": ([str(model_root / "clip")], {".safetensors"}),
        "embeddings": ([str(model_root / "embeddings")], {".pt"}),
        "upscale_models": ([str(model_root / "upscale_models")], {".pth"}),
        "custom_nodes": ([str(ROOT.parent)], set()),
    }

    def get_full_path(folder: str, name: str):
        roots = folder_paths.folder_names_and_paths.get(folder, ([], set()))[0]
        for root in roots:
            candidate = Path(root) / name
            if candidate.is_file():
                return str(candidate)
        return None

    folder_paths.get_full_path = get_full_path
    sys.modules["folder_paths"] = folder_paths


def fixture_prompt() -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "present.safetensors"}},
        "2": {"class_type": "LoraLoader", "inputs": {"lora_name": "missing-lora.safetensors", "model": ["1", 0]}},
        "3": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "seed": 123}},
        "4": {"class_type": "UnknownFancyNode", "inputs": {"image": ["999", 0], "token": "SECRET"}},
    }


def main() -> None:
    health = load_health_module()
    with tempfile.TemporaryDirectory() as temp_dir:
        model_root = Path(temp_dir)
        (model_root / "checkpoints").mkdir(parents=True)
        (model_root / "loras").mkdir(parents=True)
        (model_root / "checkpoints" / "present.safetensors").write_bytes(b"fixture")
        install_folder_paths_stub(model_root)

        resolved = health.resolve_models(fixture_prompt(), include_hashes=True)
        assert resolved["summary"]["total"] == 2, json.dumps(resolved, indent=2)
        assert resolved["summary"]["found"] == 1, json.dumps(resolved, indent=2)
        assert resolved["summary"]["missing"] == 1, json.dumps(resolved, indent=2)
        assert any(item["name"] == "present.safetensors" and item["file"].get("sha256") for item in resolved["models"])
        assert any(item["name"] == "missing-lora.safetensors" and item["suggestions"] for item in resolved["models"])

        prompt_text_fixture = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "cinematic portrait, soft light"}},
            "2": {"class_type": "GenAssetWorkflowAssistant", "inputs": {"token": "ComfyUI/user/genasset.json"}},
        }
        resolved_text = health.resolve_models(prompt_text_fixture)
        assert resolved_text["summary"]["total"] == 0, json.dumps(resolved_text, indent=2)

        diagnostics = health.diagnose_workflow(fixture_prompt(), known_node_types=["CheckpointLoaderSimple", "LoraLoader", "KSampler"])
        assert diagnostics["summary"]["error_count"] >= 3, json.dumps(diagnostics, indent=2)
        assert any(issue["kind"] == "missing_custom_node" for issue in diagnostics["issues"])
        assert any(issue["kind"] == "broken_link" for issue in diagnostics["issues"])
        assert any(issue["kind"] == "missing_model" for issue in diagnostics["issues"])

        repro = health.build_repro_lock(fixture_prompt(), {"extra": {"token": "SECRET"}})
        assert repro["schema"] == "genasset.repro_lock.v1"
        assert repro["workflow_hash"]
        repro_text = json.dumps(repro)
        assert "SECRET" not in repro_text

    print("GenAsset health tests passed.")


if __name__ == "__main__":
    main()
