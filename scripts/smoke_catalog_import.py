from __future__ import annotations

import importlib.util
import sys
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

nodes = sys.modules["ComfyUI_GenAsset.nodes"]

workflow = {"nodes": [{"id": 1, "type": "GenAssetTestConnection"}]}
wrapped = {"workflow_json": {"workflow": workflow}}

assert nodes._normalize_import_workflow_payload(workflow) == workflow
assert nodes._normalize_import_workflow_payload(wrapped) == workflow
assert nodes._validate_catalog_workflow_id("import-demo") == "import-demo"

for bad_id in ("", "../secret", "http://example.com", "bad id"):
    try:
        nodes._validate_catalog_workflow_id(bad_id)
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"Expected invalid id to fail: {bad_id!r}")

try:
    nodes._normalize_import_workflow_payload({"workflow_json": {"api_prompt": {}}})
except RuntimeError:
    pass
else:
    raise AssertionError("Expected missing visual workflow to fail.")

print("GenAsset catalog import helpers passed.")
