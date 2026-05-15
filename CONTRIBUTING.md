# Contributing to ComfyUI-GenAsset

Thanks for contributing.

This project focuses on ComfyUI nodes that connect generation workflows to GenAsset versioned memory.

## Ways to Contribute

- Improve node behavior and reliability.
- Add or improve example workflows.
- Improve docs and onboarding.
- Report bugs and suggest features.

## Local Setup

1. Clone into your ComfyUI custom nodes folder:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/steliosot/ComfyUI-GenAsset.git
```

2. Restart ComfyUI.
3. Run smoke import test:

```bash
python scripts/smoke_import.py
```

## Pull Request Guidelines

- Keep changes focused and small when possible.
- Explain user impact in the PR description.
- Add/update docs for behavior changes.
- Add/update workflow examples when relevant.
- Do not break existing node names or outputs unless discussed first.

## Workflow + Security Rules

- Never commit real tokens or secrets.
- Use placeholders in shared workflow files.
- Verify `base_url` and `token` usage in examples.
- Prefer reproducible examples (clear prompt/model/seed when useful).

## Style Expectations

- Keep node errors clear and actionable.
- Favor backward-compatible behavior.
- Keep logging/status JSON practical for troubleshooting.

## Issue Reports

Please include:

- ComfyUI version
- ComfyUI-GenAsset version
- OS + Python version
- Minimal workflow JSON to reproduce
- Relevant console logs / status JSON output

## Code of Conduct

Be respectful and constructive. We want this repo welcoming for creators, developers, and teams.
