# ComfyUI-GenAsset

ComfyUI nodes for saving and loading GenAsset generation memory.

GenAsset treats generated images as cached previews and metadata as the source of truth. These nodes let ComfyUI save a generation with its prompt, seed, model, workflow JSON, image quality metrics, and version metadata, then load that version back later for continued editing.

## Nodes

Nodes appear under the ComfyUI category:

```text
genasset
```

### Node Reference

| Node | What it does | Inputs | Outputs |
| --- | --- | --- | --- |
| `Test GenAsset Connection` | Checks that ComfyUI can reach GenAsset and that the workspace token is valid. Use this first when setting up a workflow. | `base_url`, `token` | `workspace_name`, `status_json`, `summary`, `state`, `normalized_status_json` |
| `GenAsset Workflow Assistant` | Validates a workflow and suggests optional prefill fields such as asset name, tags, intent, notes, version label, and metadata. Wire its outputs into `Save To GenAsset`. | `base_url`, `token`; optional `image`, `asset_name_hint`, `asset_id_hint` | `asset_name`, `asset_id`, `tags_csv`, `intent`, `notes_md`, `version_label`, `metadata_json`, `warnings_json`, `status_json`, `summary` |
| `Save To GenAsset` | Saves a generated image as a GenAsset version with prompt, model, seed, workflow JSON, metadata, and optional assistant-prefilled fields. | `image`, `base_url`, `token`, `asset_name`; optional `asset_id`, `tags_csv`, `intent`, `notes_md`, `version_label`, `metadata_json`, `source` | image passthrough, `asset_id`, `version_id`, `status_json` |
| `Load Asset From GenAsset` | Loads a preview image and recipe metadata. Can load an exact version, an asset's current/latest version, or the latest updated asset in the workspace. | `base_url`, `token`, optional `asset_id`, optional `version_id` | `image`, `asset_id`, `version_id`, `workflow_json`, `metadata_json`, `status_json` |
| `Load Version From GenAsset` | Loads one exact historical version by version id. | `base_url`, `token`, `version_id` | `image`, `asset_id`, `version_id`, `workflow_json`, `metadata_json`, `status_json` |
| `Save Metadata Patch To GenAsset` | Merges a JSON metadata patch into an existing version without uploading a new image. | `base_url`, `token`, `version_id`, `metadata_patch_json` | `version_id`, `metadata_json`, `status_json` |
| `Compare Two GenAsset Versions` | Loads two versions and returns both previews plus a JSON diff of key recipe fields. | `base_url`, `token`, `left_version_id`, `right_version_id` | `left_image`, `right_image`, `diff_json`, `status_json` |
| `Create Branch Version In GenAsset` | Creates a new version connected to a parent version, useful for branching experiments. | `image`, `base_url`, `token`, `asset_id`, `parent_version_id`, `asset_name`, `prompt_text`, `negative_prompt_text`, `model_name`, `seed`, `tags_csv`, `intent`, `extra_metadata_json` | image passthrough, `asset_id`, `version_id`, `status_json` |
| `Load Recipe To Widgets` | Extracts replay-friendly widget values from stored `workflow_json` and `metadata_json`. | `workflow_json`, `metadata_json` | `prompt_text`, `negative_prompt_text`, `model_name`, `seed`, `steps`, `cfg`, `sampler_name`, `scheduler`, `denoise`, `width`, `height`, `status_json` |
| `Find Assets In GenAsset` | Searches assets in the current workspace. | `base_url`, `token`, `search_query`, `page`, `page_size` | `assets_json`, `asset_ids_csv`, `status_json` |
| `List Asset Versions In GenAsset` | Lists versions for a specific asset. | `base_url`, `token`, `asset_id`, `max_versions` | `versions_json`, `version_ids_csv`, `status_json` |
| `Load Current Version For Asset` | Loads the current/latest version for one asset id. | `base_url`, `token`, `asset_id` | `image`, `asset_id`, `version_id`, `workflow_json`, `metadata_json`, `status_json` |
| `Promote Version In GenAsset` | Promotes a selected version to be the current version for an asset. | `base_url`, `token`, `asset_id`, `version_id` | `asset_id`, `version_id`, `status_json` |
| `Delete Version In GenAsset` | Deletes a GenAsset version. | `base_url`, `token`, `version_id`, `confirm_delete` | `version_id`, `status_json` |
| `Fork Asset From Version In GenAsset` | Creates a new asset from an existing source version. | `base_url`, `token`, `source_version_id`, `new_asset_name`, `prompt_suffix`, `negative_prompt_override`, `tags_csv`, `intent`, `extra_metadata_json` | `image`, `asset_id`, `version_id`, `status_json` |
| `Create Asset In GenAsset` | Creates a new GenAsset asset and initial version from a supplied image. | `image`, `base_url`, `token`, `asset_name`, `prompt_text`, `negative_prompt_text`, `model_name`, `seed`, `tags_csv`, `intent`, `extra_metadata_json` | image passthrough, `asset_id`, `version_id`, `status_json` |
| `Rename Asset In GenAsset` | Renames an existing asset. | `base_url`, `token`, `asset_id`, `new_name` | `asset_id`, `asset_name`, `status_json` |
| `Upsert Asset Tags Fields` | Updates asset-level fields such as tags and notes. | `base_url`, `token`, `asset_id`, `tags_csv`, `notes_md` | `asset_id`, `asset_json`, `status_json` |
| `Asset Summary In GenAsset` | Fetches compact summary information for one asset. | `base_url`, `token`, `asset_id` | `summary`, `asset_json`, `status_json` |

### Save Capture

`Save To GenAsset` captures the following automatically when the graph exposes it:

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

### Load Behavior

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

The nodes default to hosted GenAsset. Paste your workspace token into every GenAsset node's `token` field, or keep the default `ComfyUI/user/genasset.json` and create that file in your ComfyUI user folder.

```text
base_url = https://genasset.xyz
token = ComfyUI/user/genasset.json
asset_name = your reusable asset name
```

Recommended `user/genasset.json` format:

```json
{
  "base_url": "https://genasset.xyz",
  "workspace_token": "PASTE_TOKEN"
}
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
Loaded GenAsset nodes: GenAssetAssetSummary, ..., GenAssetWorkflowAssistant
```

## License

MIT.
