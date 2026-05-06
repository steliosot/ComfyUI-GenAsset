from __future__ import annotations

import sys
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "ComfyUI_GenAsset",
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
if spec is None or spec.loader is None:
    raise SystemExit("Could not create import spec for ComfyUI-GenAsset.")

module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
classes = sorted(module.NODE_CLASS_MAPPINGS)

expected = {"GenAssetTestConnection", "GenAssetSaveGeneration", "GenAssetLoadVersion", "GenAssetLoadAsset"}
missing = expected.difference(classes)
if missing:
    raise SystemExit(f"Missing node mappings: {', '.join(sorted(missing))}")

print(f"Loaded GenAsset nodes: {', '.join(classes)}")
