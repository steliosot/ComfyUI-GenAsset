# ComfyUI-GenAsset

ComfyUI nodes for saving and loading GenAsset generation memory.

GenAsset treats generated images as cached previews and metadata as the source of truth. These nodes let ComfyUI save a generation with its prompt, seed, model, workflow JSON, image quality metrics, and version metadata, then load that version back later for continued editing.

## Nodes

Nodes appear under the ComfyUI category:

```text
genasset
```

### Save To GenAsset

Input:

- `IMAGE`
- `base_url`
- `workspace_token`
- `asset_name`

Output:

- image passthrough
- `asset_id`
- `version_id`
- `status_json`

What it captures automatically when the graph exposes it:

- prompt and negative prompt
- checkpoint/model name
- seed, steps, cfg, sampler, scheduler, denoise
- image size and batch size
- upstream node ids
- input images from upstream `LoadImage` nodes, uploaded as GenAsset input artifacts when the files are available in the ComfyUI input folder
- ComfyUI API prompt and workflow JSON, with token-like fields redacted
- basic tags derived from the prompt/model
- image quality metrics

It refuses to save blank or near-black frames. In that case it returns a status JSON explaining the rejection instead of creating a bad version.

### Test GenAsset Connection

Input:

- `base_url`
- `workspace_token`

Output:

- workspace name
- status JSON

Use this tiny node first when helping a user. It checks that ComfyUI can reach GenAsset and that the token is valid before they run a full image workflow.

### Load Asset From GenAsset

Input:

- `base_url`
- `workspace_token`
- `asset_id` (optional)
- `version_id` (optional)

Output:

- preview image
- `asset_id`
- `version_id`
- workflow JSON
- metadata JSON
- status JSON

Behavior:

- If `version_id` is set: loads that exact version.
- Else if `asset_id` is set: loads the current/latest version of the matched asset.
- Else (both empty): loads the latest updated asset in the workspace.

`asset_id` should be the exact asset id (UUID). Leave it empty to load the latest asset in the workspace.

## Install

### ComfyUI Manager

After the node pack is published to the Comfy Registry, open ComfyUI Manager and search for:

```text
GenAsset
```

Install the node pack, restart ComfyUI, and the nodes should appear under `genasset`.

If you are loading a workflow that contains GenAsset nodes, ComfyUI-Manager's
missing-node installer should resolve those node types to the `genasset`
package from the default channel.

### Manual Git Install

From your ComfyUI folder:

```bash
cd custom_nodes
git clone https://github.com/steliosot/ComfyUI-GenAsset.git
```

Restart ComfyUI. The nodes should appear under `genasset`.

No separate Python dependencies are required beyond the normal ComfyUI runtime.

## Connect To GenAsset

In GenAsset:

1. Open `Settings`.
2. Select `Tokens`.
3. Create a workspace token.
4. Paste it into `Test GenAsset Connection`.
5. If the test succeeds, paste the same token into `Save To GenAsset` or `Load Asset From GenAsset`.

The nodes default to hosted GenAsset. Paste your workspace token into every GenAsset node:

```text
base_url = https://genasset.xyz
workspace_token = PASTE_TOKEN
asset_name = your reusable asset name
```

For local development, replace `base_url` with your local app URL, for example `http://127.0.0.1:3010`.

## Example Workflows

Example workflows are in [`workflows/`](workflows/):

- `genasset_sdxl_save_generation.json`
  - Generate an image and save it to GenAsset.
- `genasset_load_version.json`
  - Load latest by default, or an exact version when `version_id` is set.
- `genasset_img2img_load_edit_save_woman_cafe.json`
  - Load an asset, run img2img, and save the edit as the next version.

Typical round trip:

```text
Generate in ComfyUI
  -> Save To GenAsset
  -> Browse asset/version in GenAsset
  -> Load Asset From GenAsset
  -> VAE Encode
  -> KSampler
  -> VAE Decode
  -> Save To GenAsset
```

## Security Notes

- Workspace tokens are sent only to the configured GenAsset API as bearer tokens.
- Saved workflow JSON redacts fields named like `workspace_token`, `token`, `authorization`, `api_key`, `secret`, and `service_role_key`.
- Do not commit workflow files after pasting real workspace tokens into them.
- Example workflows in this repo use placeholders only.

## Smoke Test

From this repo:

```bash
python scripts/smoke_import.py
```

Expected result:

```text
Loaded GenAsset nodes: GenAssetLoadVersion, GenAssetSaveGeneration, GenAssetTestConnection
```

## License

MIT.
