
""" Texture utilities. Separate module to keep compatibility Channel Packer version-agnostic. """

import os
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple, Any
from collections import defaultdict
from functools import lru_cache
import importlib.util

from settings import SIZE_SUFFIXES

from backend.texture_classes  import (TextureMapData, MapNameAndResolution)

from backend.image_lib import (close_image, from_array_u8)


LOG_TYPES: list[str] = ["info", "warn", "error", "skip", "complete"]
# Defines log types; the backend handles printing for the Windows CLI and Unreal Engine.

def log(message: str, message_kind: LOG_TYPES = "info") -> None:
# Maps different log types.

    if message == "":
        print("")
        return

    if message_kind not in LOG_TYPES:
        message_kind = "info"

    if message_kind == "info":
        print(f"   {message}")
    elif message_kind == "warn":
        print(f"⚠️ {message}")
    elif message_kind == "error":
        print(f"⛔ {message}")
    elif message_kind == "skip":
        print(f"❌ {message}")
    elif message_kind == "complete":
        print(f"✅ {message}")
    else:
        print(message)  # fallback

    # Print styles:
    # info: 3 whitespaces + message
    # warn: ⚠️ + message
    # error: ⛔ + message
    # skip: ❌ + message
    # complete: ✅ + message


def check_texture_suffix_mismatch(texture: TextureMapData) -> Optional[MapNameAndResolution]:
# Checks a single texture if its declared size suffix in the name (if present) matches its actual resolution.

    if not getattr(texture, "resolution", None):
        return None
    declared_suffix: str = (texture.suffix or "").lower().lstrip("_")
    declared_suffix = re.split(r"[-_.]", declared_suffix, maxsplit=1)[0]
    expected_suffix: str = resolution_to_suffix(texture.resolution).lower().lstrip("_")
    if declared_suffix and declared_suffix != expected_suffix:
        return MapNameAndResolution(texture.filename, texture.resolution)
    return None


@lru_cache(maxsize=1)
def check_exr_libraries() -> bool:
# Checks if OpenEXR and Numpy are installed for processing the .exr files.

    try:
        has_openexr = (importlib.util.find_spec("OpenEXR") is not None)
        has_numpy   = (importlib.util.find_spec("numpy") is not None)
        return bool(has_openexr and has_numpy)
    except Exception:
        return False


def close_image_files(images: Iterable[Optional[object]]) -> None:
# Safely closes all opened images even if there is an error during image processing.

    processed_ids: Set[int] = set()
    for image in images:
        if image is None:
            continue
        image_id = id(image)
        if image_id in processed_ids:
            continue
        processed_ids.add(image_id)
        try:
            close_image(image) # Function from image_lib
        except (OSError, ValueError):
            pass


def convert_exr_to_image(source_exr_path: str, *, file_extension: str = "png", delete_source_files: bool = False, srgb_transform: bool = False) -> Optional[str]:
# Converts 32bit float .exr image to 8bit int image using OpenEXR and Numpy.

    try:
        import numpy as np
        import OpenEXR
        import Imath

        from numpy.typing import NDArray

# Preparing the image:
        file: OpenEXR.InputFile = OpenEXR.InputFile(source_exr_path)
        hdr: dict[str, Any] = file.header()
        data_window: Imath.Box2i = hdr['dataWindow']
        width: int = data_window.max.x - data_window.min.x + 1
        height: int = data_window.max.y - data_window.min.y + 1
        float_pixel_data: Imath.PixelType = Imath.PixelType(Imath.PixelType.FLOAT) # Setting pixel data type to float.

        channels_list: list[str] = list(hdr['channels'].keys())
        channel_names: dict[str, str] = {channel.lower(): channel for channel in channels_list}
        # Gets names of all available channels.

        def linear_to_srgb(linear_values: NDArray[np.float32]) -> NDArray[np.float32]:
        # Applies sRGB gamma.
            linear_values = np.clip(linear_values, 0.0, 1.0).astype(np.float32)
            srgb_a = 0.055
            return np.where(linear_values <= 0.0031308, linear_values * 12.92, (1 + srgb_a) * np.power(linear_values, 1 / 2.4) - srgb_a)

        is_rgb: bool = all(k in channel_names for k in ("r", "g", "b"))
        has_alpha: bool = ("a" in channel_names)

        def read_channel(channel_name: str) -> NDArray[np.float32]:
        # Reads chanel as a 32b float and restructure its pixels into 2D array W*H.
            return np.frombuffer(file.channel(channel_name, float_pixel_data), dtype=np.float32).reshape(height, width)



# Processing the image:
        if is_rgb:
            f64_to_f32: NDArray[np.float32]
            r: NDArray[np.float32]
            g: NDArray[np.float32]
            b: NDArray[np.float32]
            alpha: NDArray[np.float32]
            rgb: NDArray[np.float32]

            r, g, b = read_channel(channel_names["r"]), read_channel(channel_names["g"]), read_channel(channel_names["b"])
            rgb = np.stack([r, g, b], axis=-1) # Creates a NumPy array combining all RGB channels: HxWx3 (Height, Width, Channels).

            if has_alpha:
                alpha: NDArray[np.float32] = read_channel(channel_names["a"])[..., None]
                eps: float = 1e-6
                alpha_min: float = float(alpha.min())
                alpha_max: float = float(alpha.max())
                almost_empty_alpha: bool = alpha_max <= eps
                almost_opaque_alpha: bool = alpha_min >= 1.0 - eps
                if not almost_empty_alpha and not almost_opaque_alpha:
                    partial_alpha_fraction: float = float(((alpha > eps) & (alpha < 1.0 - eps)).mean())
                    if partial_alpha_fraction > 1e-3:
                        alpha_denominator: NDArray[np.float32] = np.maximum(alpha, np.float32(1e-8))
                        rgb = np.divide(rgb, alpha_denominator, out=rgb, where=alpha_denominator > 0).astype(np.float32)
                    # Un-premultiplies RGB using a non-zero alpha divisor.
            # Un-premultiplies Alpha if available, and is neither all 0 nor 1.

            if srgb_transform:
                rgb = linear_to_srgb(rgb)

            output_image_u8: np.ndarray[np.uint8] = np.rint(np.clip(rgb, 0, 1) * 255.0).astype("uint8") # Converting to 8bit int.
            generated_image = from_array_u8(output_image_u8, "RGB") # Generating the image.
        # Converting the RGB file.

        else:
            grayscale = read_channel(channels_list[0])
            if srgb_transform:
                grayscale = linear_to_srgb(grayscale)

            output_image_u8: np.ndarray[np.uint8] = np.rint(np.clip(grayscale, 0, 1) * 255.0).astype("uint8") # Converting to 8bit int.
            generated_image = from_array_u8(output_image_u8, "L") # Generating the image.
        # Converting the Grayscale file.
        # In case the full RGB is missing, it extracts the first available channel.


# Creating the file:
        source_filename: tuple[str, str] = os.path.splitext(source_exr_path)
        filename, _ = source_filename
        target_path: str = f"{filename}.{file_extension}" # The final file path.
        save_kwargs: dict[str, object] = {}

        if file_extension == "jpeg":
            save_kwargs.setdefault("quality", 95)
            save_kwargs.setdefault("optimize", True)

        generated_image.save(target_path, format=file_extension.upper(), **save_kwargs)

        if delete_source_files:
            try:
                os.remove(source_exr_path)
            except Exception:
                pass
        # Deletes an original .exr file.
        return os.path.abspath(target_path).replace("\\", "/")

    except Exception as error:
        log(f".exr to .{file_extension} failed for '{source_exr_path}': {error}", "error")
        return None


def detect_size_suffix(name: str) -> str:
# Detects size suffixes present in the map name, e.g., "2K"

    normalized_size_suffixes: List[str] = sorted([size_suffix.lower() for size_suffix in SIZE_SUFFIXES if size_suffix], key=len, reverse=True)
    # Normalizes tokens to lowercase and sorts by reverse length to avoid shorter tokens matching before longer ones.
    if not normalized_size_suffixes:
        return ""
    pattern = r"(?:[\._\-])(" + "|".join(map(re.escape, normalized_size_suffixes)) + r")$"
    # Tries to match suffix variants to the map name
    matched_suffix: Optional[re.Match[str]] = re.search(pattern, name.lower())
    if matched_suffix:
        return matched_suffix.group(1)
    alt_pattern: str = r"(?:[\._\-])(" + "|".join(map(re.escape, normalized_size_suffixes)) + r")(?:-[a-z0-9]+)?(?=[\._\-][a-z0-9]+$)"
    alt_match: Optional[re.Match[str]] = re.search(alt_pattern, name.lower())
    return alt_match.group(1) if alt_match else ""
    # Returns the captured token e.g., '2k' if able to find one


def group_paths_by_folder(source_paths: Iterable[str]) -> Dict[str, List[str]]:
# Groups all file paths according to their relative path.
# Root is set to work_dir in list_initial_files
# E.g., A/B/T_Wall_AO.png > A/B: A/B/T_Wall_AO.png

    paths_by_folder: Dict[str, List[str]] = defaultdict(list)

    for path in source_paths:
        if not isinstance(path, str) or not path:
            continue

        normalized_path: str = path.replace("\\", "/")
        parent_directory = os.path.dirname(normalized_path).replace("\\", "/") or "."
        paths_by_folder[parent_directory].append(path)
    return {folder: sorted(paths) for folder, paths in sorted(paths_by_folder.items(), key=lambda kv: kv[0])}


def is_power_of_two(n: int) -> bool:
# Returns True if n is a power of two (n > 0).
    return (n & (n - 1) == 0) and n != 0


def make_output_dirs(base_directory: str, * , target_folder_name: Optional[str], backup_folder_name: Optional[str]) -> tuple[str, Optional[str]]:
# Creates/returns the output and optional backup directories for a given base path:

    base_directory = os.path.abspath(base_directory or ".")

    target_folder_name = (target_folder_name or "").strip()
    target_folder_directory = os.path.join(base_directory, target_folder_name) if target_folder_name else base_directory
    os.makedirs(target_folder_directory, exist_ok=True)

    backup_folder_directory = None
    backup_folder_name = (backup_folder_name or "").strip()
    if backup_folder_name:
        backup_folder_directory = os.path.join(base_directory, backup_folder_name)
        os.makedirs(backup_folder_directory, exist_ok=True)

    return target_folder_directory, backup_folder_directory


def match_suffixes(name_lower: str, type_suffix: str, size_suffix: str) -> Optional[str]:
# Takes into account different naming conventions, returns the regex pattern that matches one.
# Type...size, size...type, ...type

    separator: str = r"[\_\-\.]"
    middle_text: str = rf"(?:{separator}[A-Za-z0-9]+)?"

    if size_suffix:
        pattern1: str = rf"{separator}{re.escape(type_suffix)}{middle_text}{separator}{re.escape(size_suffix)}$"  # type ... [middle_text] ... size
        pattern2: str = rf"{separator}{re.escape(size_suffix)}{middle_text}{separator}{re.escape(type_suffix)}$"  # size ... [middle_text] ... type
        # Pattern3 = if more variations are necessary.
        if re.search(pattern1, name_lower):
            return pattern1
        if re.search(pattern2, name_lower):
            return pattern2
        # Returns the first matching pattern string.

    only_type_suffix: str = rf"{separator}{re.escape(type_suffix)}$"
    if re.search(only_type_suffix, name_lower):
        return only_type_suffix
    # Returns this in case only the type suffix is present.
    return None


def resolution_to_suffix(size: Tuple[int, int]) -> str:
# Tries to match the actual image size to a size suffix.

    width = max(size)
    for threshold, label in [
        (512, "512"), (1024, "1K"), (2048, "2K"), (4096, "4K"), (8192, "8K")
    ]:
        if width <= threshold:
            return label
        # Returns the full size if it does not match any suffix threshold.
    return f"{width}px"


def validate_safe_folder_name(raw_folder_name: Optional[str]) -> None:
# Validates that the custom folder name doesn't include unsupported characters.

    folder_name: str = (raw_folder_name or "")
    if folder_name.strip() == "":
        return

    if any(invalid_character in folder_name for invalid_character in '\\/:*?"<>|'):
        log(f"Aborted: invalid folder name '{raw_folder_name}'. It cannot contain \\ / : * ? \" < > |", "error")
        # Prints error.
        raise SystemExit(1)
    return