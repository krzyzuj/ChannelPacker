
""" Channel Packer settings. """

import json
import os
from typing import List, Tuple

from backend.texture_classes import PackingMode, TextureTypeConfig


def _as_bool(v) -> bool:
# Converts .json input (bool/int/str/None) to a real bool;
# Avoids the case where a non-empty string like "False" is treated as True.

    if isinstance(v, bool): return v
    if isinstance(v, str):
        input_str = v.strip().lower()
        if input_str == "": return False
        return input_str in ("1","true","yes","on")
    return bool(v)



#                                           === Loading JSON file ===

_config_path = os.path.join(os.path.dirname(__file__), "config.json")
with open(_config_path, "r", encoding="utf-8") as f:
    _config_data = json.load(f)


# Assigning config values:
INPUT_FOLDER: str = _config_data.get("INPUT_FOLDER", "").strip() # Folder containing textures to be packed.
FILE_TYPE: str = _config_data.get("FILE_TYPE", "png") # File type of generated channel-packed textures.
DELETE_USED: bool = _as_bool(_config_data.get("DELETE_USED", False))  # Deletes the files used for channel packing.
TARGET_FOLDER_NAME: str = _config_data.get("DEST_FOLDER_NAME", "created_maps") # If provided, places generated channel-packed maps into a custom folder.
BACKUP_FOLDER_NAME: str = _config_data.get("BACKUP_FOLDER_NAME", "") # If provided, moves source maps used during generation into a backup folder after creating the channel-packed map.
EXR_SRGB_CURVE: bool = _as_bool(_config_data.get("EXR_SRGB_CURVE", True)) # If true, applies sRGB gamma transform when converting the .exr, mimicking Photoshop behavior, when converting with gamma 1.0/exposure 0.0
RESIZE_STRATEGY: str = _config_data.get("RESIZE_STRATEGY", "down") # Specifies how textures are rescaled when resolutions differ within a set: down to the smallest or up to the largest.
PACKING_MODES: list[PackingMode] = _config_data.get("PACKING_MODES", []) # Uses TEXTURE_CONFIG keys for texture maps to be put into channels. The packing mode is skipped if "name": is empty.

SHOW_DETAILS: bool = _as_bool(_config_data.get("SHOW_DETAILS", False)) # Shows details like exact resolution when printing logs.




#                                           === Constants ===

ALLOWED_FILE_TYPES: Tuple[str, ...] = ("png", "jpg", "jpeg", "tga")
SIZE_SUFFIXES: List[str] = ["512", "1k", "2k", "4k", "8k", ""]

TEXTURE_CONFIG: dict[str, TextureTypeConfig] = {
    "AO": {"suffixes": ["ambientocclusion", "occlusion", "ambient", "ao"], "default": ("G", 255)},
    "Roughness": {"suffixes": ["roughness", "roughnes", "rough", "r"], "default": ("G", 128)},
    "Metalness": {"suffixes": ["metalness", "metalnes", "metallic", "metal", "m"], "default": ("G", 0)},
    "Height": {"suffixes": ["displacement", "height", "disp", "d", "h"], "default": ("G", 0)},
    "Mask": {"suffixes": ["opacity", "alpha", "mask"], "default": ("G", 255)},
    "Translucency": {"suffixes": ["translucency", "translucent", "trans", "t"], "default": ("G", 0)},
    "Specular": {"suffixes": ["specular", "spec", "s"], "default": ("G", 128)},
    "Normal": {"suffixes": ["normal_dx", "normal_gl", "normaldx", "normalgl", "normalgl", "normal", "nor_dx", "nor_gl", "norm", "nrm", "n"], "default": ("RGB", 128)},
    "BendNormal": {"suffixes": ["bend_normal", "bendnormal", "bn"], "default": ("RGB", 128)},
    "Bump": {"suffixes": ["bump", "bp"], "default": ("G", 128)},
    "Albedo": {"suffixes": ["basecolor", "diffuse", "albedo", "color", "diff", "base", "a", "b"],  "default": ("RGB", 128)},
    "SSS": {"suffixes": ["subsurface", "sss"], "default": ("G", 0)},
    "Emissive": {"suffixes": ["emissive", "emission", "emit", "glow"], "default": ("RGB", 0)},
    "Glossiness": {"suffixes": ["glossiness", "gloss", "gl"], "default": ("G", 128)}}
# The G/RGB image type is used by validate_packing_modes to ensure that an RGB image is not mapped to a single channel without explicitly specifying the channel using .R or _R.