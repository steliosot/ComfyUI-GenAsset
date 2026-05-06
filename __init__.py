from .nodes import GenAssetLoadAsset, GenAssetLoadVersion, GenAssetSaveGeneration, GenAssetTestConnection

NODE_CLASS_MAPPINGS = {
    "GenAssetTestConnection": GenAssetTestConnection,
    "GenAssetSaveGeneration": GenAssetSaveGeneration,
    "GenAssetLoadVersion": GenAssetLoadVersion,
    "GenAssetLoadAsset": GenAssetLoadAsset,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GenAssetTestConnection": "Test GenAsset Connection",
    "GenAssetSaveGeneration": "Save To GenAsset",
    "GenAssetLoadVersion": "Load From GenAsset",
    "GenAssetLoadAsset": "Load Asset From GenAsset",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
