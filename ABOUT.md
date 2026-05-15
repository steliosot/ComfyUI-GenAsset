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

## Who It Is For

- ComfyUI creators
- Creative teams
- Tool builders
- Agent workflows (MCP / SDK / automation)

## Project Links

- Start free: https://www.genasset.xyz/
- Repository: https://github.com/steliosot/ComfyUI-GenAsset
- Issues: https://github.com/steliosot/ComfyUI-GenAsset/issues
