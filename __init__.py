from .nodes import GenAssetLoadVersion, GenAssetSaveGeneration, GenAssetTestConnection

NODE_CLASS_MAPPINGS = {
    "GenAssetTestConnection": GenAssetTestConnection,
    "GenAssetSaveGeneration": GenAssetSaveGeneration,
    "GenAssetLoadVersion": GenAssetLoadVersion,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GenAssetTestConnection": "Test GenAsset Connection",
    "GenAssetSaveGeneration": "Save To GenAsset",
    "GenAssetLoadVersion": "Load Asset From GenAsset",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
