from typing import Dict, TypedDict, Optional, Tuple, List
from dataclasses import dataclass, field


class ChannelMapping(TypedDict):
    R: Optional[str]
    G: Optional[str]
    B: Optional[str]
    A: Optional[str]

class PackingMode(TypedDict):
    mode_name: str # Packing mode name defined in the config; the mode is skipped if left empty.
    custom_suffix: str # If empty, uses a generated default suffix from the first letters of the mapped channels
    channels: ChannelMapping # Texture map types for channel packing, mapped to RGBA channels.

class TextureData(TypedDict):
    texture_set_name: str # Case-sensitive texture set name after stripping type/size suffixes.
    extension: str # File extension type.
    path: str # File path.
    resolution: Tuple[int, int] # Texture resolution read from the file.
    texture_type: str # Texture map type, e.g., "Albedo"
    declared_suffix: str # Declared size suffix in the filename (if present).
    filename: str # Original case-sensitive filename.

class TextureTypeConfig(TypedDict):
    suffixes: list[str] # Possible suffixes for a given texture map type, e.g., ["ao", "ambientocclusion", "occlusion", "ambient"].
    default: tuple[str, int]  # Stores default values for a grayscale or RGB image.


class SetEntry(TypedDict):
    texture_set_name: str # Case-sensitive texture set name after stripping type/size suffixes.
    texture_types: Dict[str, List[str]] # Collection of recognized texture map types e.g., "Albedo" grouped by a texture set name.
    untyped: List[str]  # Files whose texture type couldn't be recognized from the filename (suffix not matched).


@dataclass
class MapNameAndResolution:
    filename: str # Original case-sensitive filename.
    resolution: Tuple[int, int] # Texture resolution.

@dataclass
class TextureMapData:
    file_path: str # File path.
    resolution: Tuple[int, int] # Texture resolution.
    suffix: str # Declared size suffix.
    filename: str # Case-sensitive file name.

TextureMapCollection = Dict[str, TextureMapData] # Maps a texture map type to its data, e.g., "Albedo": [(path="", resolution=(,), suffix="", filename="", ext="")]
# Can include different sizes for the same map type (e.g., Albedo 2K and 4K) if a set contains both; later, only the largest size is used.

@dataclass
class TextureSet:
    texture_set_name: str  # Case-sensitive texture set name.
    available_texture_maps: TextureMapCollection = field(default_factory=dict) # Lists texture map types found for the set as keys, with their extracted data as values (e.g., "Albedo" → {path, resolution, suffix, filename}).
    processed: bool = False # Indicates whether at least one packing mode was processed successfully for this set.
    completed: bool = False # Indicates whether this texture set has been fully processed or skipped.

@dataclass
class ValidModeEntry:
    texture_set_name: str # Case-sensitive name of the texture set.
    mode: PackingMode  # Packing mode configuration selected for this set.
    texture_maps_for_mode: TextureMapCollection # Maps required by the mode, filtered for this set.
    packing_mode_suffix: str # Final suffix used in the output filename for this mode (custom or generated).

@dataclass
class TextureSetInfo:
    texture_set_name: str # Case-sensitive texture set name
    texture_type: str # Texture map type, e.g., "Albedo"
    declared_suffix: str # Declared size suffix in the filename (if present).
    original_filename: str # Original case-sensitive filename.



