from .nodes import (
    GenAssetAssetSummary,
    GenAssetCompareVersions,
    GenAssetCreateAsset,
    GenAssetCreateBranchVersion,
    GenAssetDeleteVersion,
    GenAssetFindAssets,
    GenAssetForkAssetFromVersion,
    GenAssetLoadExactVersion,
    GenAssetLoadCurrentVersion,
    GenAssetLoadRecipeToWidgets,
    GenAssetLoadVersion,
    GenAssetListAssetVersions,
    GenAssetPatchVersionMetadata,
    GenAssetPromoteVersion,
    GenAssetRenameAsset,
    GenAssetSaveGeneration,
    GenAssetTestConnection,
    GenAssetUpsertAssetFields,
    GenAssetWorkflowAssistant,
)

try:
    from .server import register_routes

    register_routes()
except Exception as exc:
    print(f"[GenAsset] Health routes unavailable: {exc}")

NODE_CLASS_MAPPINGS = {
    "GenAssetTestConnection": GenAssetTestConnection,
    "GenAssetWorkflowAssistant": GenAssetWorkflowAssistant,
    "GenAssetSaveGeneration": GenAssetSaveGeneration,
    "GenAssetLoadVersion": GenAssetLoadVersion,
    "GenAssetLoadExactVersion": GenAssetLoadExactVersion,
    "GenAssetPatchVersionMetadata": GenAssetPatchVersionMetadata,
    "GenAssetCompareVersions": GenAssetCompareVersions,
    "GenAssetCreateBranchVersion": GenAssetCreateBranchVersion,
    "GenAssetLoadRecipeToWidgets": GenAssetLoadRecipeToWidgets,
    "GenAssetFindAssets": GenAssetFindAssets,
    "GenAssetListAssetVersions": GenAssetListAssetVersions,
    "GenAssetLoadCurrentVersion": GenAssetLoadCurrentVersion,
    "GenAssetPromoteVersion": GenAssetPromoteVersion,
    "GenAssetDeleteVersion": GenAssetDeleteVersion,
    "GenAssetForkAssetFromVersion": GenAssetForkAssetFromVersion,
    "GenAssetCreateAsset": GenAssetCreateAsset,
    "GenAssetRenameAsset": GenAssetRenameAsset,
    "GenAssetUpsertAssetFields": GenAssetUpsertAssetFields,
    "GenAssetAssetSummary": GenAssetAssetSummary,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GenAssetTestConnection": "Test GenAsset Connection",
    "GenAssetWorkflowAssistant": "GenAsset Workflow Assistant",
    "GenAssetSaveGeneration": "Save To GenAsset",
    "GenAssetLoadVersion": "Load Asset From GenAsset",
    "GenAssetLoadExactVersion": "Load Version From GenAsset",
    "GenAssetPatchVersionMetadata": "Save Metadata Patch To GenAsset",
    "GenAssetCompareVersions": "Compare Two GenAsset Versions",
    "GenAssetCreateBranchVersion": "Create Branch Version In GenAsset",
    "GenAssetLoadRecipeToWidgets": "Load Recipe To Widgets",
    "GenAssetFindAssets": "Find Assets In GenAsset",
    "GenAssetListAssetVersions": "List Asset Versions In GenAsset",
    "GenAssetLoadCurrentVersion": "Load Current Version For Asset",
    "GenAssetPromoteVersion": "Promote Version In GenAsset",
    "GenAssetDeleteVersion": "Delete Version In GenAsset",
    "GenAssetForkAssetFromVersion": "Fork Asset From Version In GenAsset",
    "GenAssetCreateAsset": "Create Asset In GenAsset",
    "GenAssetRenameAsset": "Rename Asset In GenAsset",
    "GenAssetUpsertAssetFields": "Upsert Asset Tags Fields",
    "GenAssetAssetSummary": "Asset Summary In GenAsset",
}

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
