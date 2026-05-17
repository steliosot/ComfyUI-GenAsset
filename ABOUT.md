# About ComfyUI-GenAsset

ComfyUI-GenAsset is a custom node pack that connects ComfyUI workflows to **GenAsset**.

## What It Solves

Image generation teams often lose reproducibility when prompts, models, and settings drift over time. This node pack helps preserve the full recipe around each output so it can be loaded, compared, edited, and versioned later.

## Core Idea

- Image output is saved with context.
- Recipe and metadata are treated as first-class.
- Every save can become a reusable version in a creative lineage.

## Typical Use Cases

- Save approved versions from txt2img/img2img runs.
- Reload exact historical versions for consistency.
- Compare two versions before approval.
- Branch/fork creative directions while keeping parent lineage.
- Add metadata without re-rendering.
- Inspect workflow health before queueing or sharing.
- Resolve missing model files and expected ComfyUI folders.
- Preserve Repro Lock metadata for the environment that created each saved image.

## Health And Reproducibility

The GenAsset Manager includes a Health tab for workflow checks:

- Model Resolver scans the current graph for checkpoints, UNets/GGUFs, LoRAs, VAEs, ControlNets, CLIP files, embeddings, and upscale models.
- Workflow Doctor sends a redacted health payload through GenAsset AI and returns plain-language issues and fix suggestions.
- Repro Lock preview shows the metadata that Save To GenAsset automatically attaches to each saved generation.

`Save To GenAsset` now stores Repro Lock metadata automatically under `metadata.repro_lock`, including the workflow hash, ComfyUI and custom-node version context, Python/Torch/CUDA/MPS details, and model file metadata when available.

## Who It Is For

- ComfyUI creators
- Creative teams
- Tool builders
- Agent workflows (MCP / SDK / automation)

## Project Links

- Start free: https://www.genasset.xyz/
- Repository: https://github.com/steliosot/ComfyUI-GenAsset
- Issues: https://github.com/steliosot/ComfyUI-GenAsset/issues
