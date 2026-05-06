from .nodes import GenAssetLoadAsset, GenAssetLoadVersion, GenAssetSaveGeneration

NODE_CLASS_MAPPINGS = {
    "GenAssetSaveGeneration": GenAssetSaveGeneration,
    "GenAssetLoadVersion": GenAssetLoadVersion,
    "GenAssetLoadAsset": GenAssetLoadAsset,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GenAssetSaveGeneration": "Save To GenAsset",
    "GenAssetLoadVersion": "Load From GenAsset",
    "GenAssetLoadAsset": "Load Asset From GenAsset",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
