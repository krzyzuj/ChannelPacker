
""" Texture utilities. Separate module to keep compatibility Channel Packer version-agnostic. """

import os
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple, Any
from collections import defaultdict
from functools import lru_cache
import importlib.util

from settings import (BACKUP_FOLDER_NAME, DEST_FOLDER_NAME, SIZE_SUFFIXES)

from backend.texture_classes  import (TextureMapData, MapNameAndRes)

from backend.image_lib import (close_image, from_array_u8)


LOG_TYPES: list[str] = ["info", "warn", "error", "skip", "complete"]
# Defines log types; the backend handles printing for the Windows CLI and Unreal Engine.

def log(msg: str, kind: LOG_TYPES = "info") -> None:
# Maps different log types.

    if msg == "":
        print("")
        return

    if kind not in LOG_TYPES:
        kind = "info"

    if kind == "info":
        print(f"   {msg}")
    elif kind == "warn":
        print(f"⚠️ {msg}")
    elif kind == "error":
        print(f"⛔ {msg}")
    elif kind == "skip":
        print(f"❌ {msg}")
    elif kind == "complete":
        print(f"✅ {msg}")
    else:
        print(msg)  # fallback

    # Print styles:
    # info: 3 whitespaces + msg
    # warn: ⚠️ + msg
    # error: ⛔ + msg
    # skip: ❌ + msg
    # complete: ✅ + msg


def check_texture_suffix_mismatch(tex: TextureMapData) -> Optional[MapNameAndRes]:
# Checks a single texture if its declared size suffix in the name (if present) matches its actual resolution.

    if not getattr(tex, "resolution", None):
        return None
    declared_suffix = (tex.suffix or "").lower().lstrip("_")
    expected_suffix = resolution_to_suffix(tex.resolution).lower().lstrip("_")
    if declared_suffix and declared_suffix != expected_suffix:
        return MapNameAndRes(tex.filename, tex.resolution)
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

    seen: Set[int] = set()
    for im in images:
        if im is None:
            continue
        oid = id(im)
        if oid in seen:
            continue
        seen.add(oid)
        try:
            close_image(im) # Function from image_lib
        except (OSError, ValueError):
            pass


def convert_exr_to_image(src_exr: str, *, ext: str = "png", delete_src: bool = False, srgb_transform: bool = False) -> Optional[str]:
# Converts 32bit float .exr image to 8bit int image using OpenEXR and Numpy.

    try:
        import numpy as np
        import OpenEXR
        import Imath

        from numpy.typing import NDArray

# Preparing the image:
        file: OpenEXR.InputFile = OpenEXR.InputFile(src_exr)
        hdr: dict[str, Any] = file.header()
        dw: Imath.Box2i = hdr['dataWindow']
        width: int = dw.max.x - dw.min.x + 1
        height: int = dw.max.y - dw.min.y + 1
        float_pix: Imath.PixelType = Imath.PixelType(Imath.PixelType.FLOAT) # Setting pixel data type to float.

        channels_list: list[str] = list(hdr['channels'].keys())
        ch_names: dict[str, str] = {n.lower(): n for n in channels_list}
        # Gets names of all available channels.

        def read_channel(n: str) -> NDArray[np.float32]:
        # Reads chanel as a 32b float and restructure its pixels into 2D array W*H.
            return np.frombuffer(file.channel(n, float_pix), dtype=np.float32).reshape(height, width)

        def linear_to_srgb(x: NDArray[np.float32]) -> NDArray[np.float32]:
        # Applies sRGB gamma.
            x = np.clip(x, 0.0, 1.0).astype(np.float32)
            a = 0.055
            return np.where(x <= 0.0031308, x * 12.92, (1 + a) * np.power(x, 1 / 2.4) - a)

        has_rgb: bool = all(k in ch_names for k in ("r", "g", "b"))
        has_a: bool = ("a" in ch_names)


# Processing the image:
        if has_rgb:
            f64_to_f32: NDArray[np.float32]
            r: NDArray[np.float32]
            g: NDArray[np.float32]
            b: NDArray[np.float32]
            a: NDArray[np.float32]
            rgb: NDArray[np.float32]

            r, g, b = read_channel(ch_names["r"]), read_channel(ch_names["g"]), read_channel(ch_names["b"])
            rgb = np.stack([r, g, b], axis=-1) # Creates a NumPy array combining all RGB channels: HxWx3 (Height, Width, Channels).

            if has_a:
                a = read_channel(ch_names["a"])[..., None]
                eps = 1e-6
                if not (float(a.max()) <= eps or float(a.min()) >= 1.0 - eps):
                    f64_to_f32 = np.maximum(a, np.float32(1e-8))
                    rgb = np.divide(rgb, f64_to_f32, out=rgb, where=f64_to_f32 > 0).astype(np.float32)
            # Un-premultiplies Alpha if available, and is neither all 0 nor 1.

            if srgb_transform:
                rgb = linear_to_srgb(rgb)

            out_u8: np.ndarray[np.uint8] = np.rint(np.clip(rgb, 0, 1) * 255.0).astype("uint8") # Converting to 8bit int.
            gen_image = from_array_u8(out_u8, "RGB") # Generating the image.
        # Converting the RGB file.

        else:
            y = read_channel(channels_list[0])
            if srgb_transform:
                y = linear_to_srgb(y)

            out_u8: np.ndarray[np.uint8] = np.rint(np.clip(y, 0, 1) * 255.0).astype("uint8") # Converting to 8bit int.
            gen_image = from_array_u8(out_u8, "L") # Generating the image.
        # Converting the Grayscale file.
        # In case the full RGB is missing, it extracts the first available channel.


# Creating the file:
        file_name: tuple[str, str] = os.path.splitext(src_exr)
        base, _ = file_name
        dst: str = f"{base}.{ext}" # The final file path.
        save_kwargs: dict[str, object] = {}

        if ext =="jpeg":
            save_kwargs.setdefault("quality", 95)
            save_kwargs.setdefault("optimize", True)

        gen_image.save(dst, format=ext.upper(), **save_kwargs)

        if delete_src:
            try:
                os.remove(src_exr)
            except Exception:
                pass
        # Deletes an original .exr file.

        return os.path.abspath(dst).replace("\\", "/")

    except Exception as e:
        log(f".exr to .{ext} failed for '{src_exr}': {e}", "error")
        return None


def detect_size_suffix(name: str) -> str:
# Detects size suffixes present in the map name,  e.g., "2K"

    tokens: List[str] = sorted([s.lower() for s in SIZE_SUFFIXES if s], key=len, reverse=True)
    # Normalizes tokens to lowercase and sorts by reverse length to avoid shorter tokens matching before longer ones.
    if not tokens:
        return ""
    pattern = r"(?:[\._\-])(" + "|".join(map(re.escape, tokens)) + r")$"
    # Tries to match suffix variants to the map name
    m: Optional[re.Match[str]] = re.search(pattern, name.lower())
    return m.group(1) if m else ""
    # Returns the captured token e.g., '2k' if able to find one


def group_paths_by_folder(keys: Iterable[str]) -> Dict[str, List[str]]:
# Groups all file paths according to their relative path.
# Root is set to work_dir in list_initial_files
# E.g., A/B/T_Wall_AO.png > A/B: A/B/T_Wall_AO.png

    groups: Dict[str, List[str]] = defaultdict(list)

    for key in keys:
        if not isinstance(key, str) or not key:
            continue

        k: str = key.replace("\\", "/")
        parent = os.path.dirname(k).replace("\\", "/")
        label = parent if parent else "."
        groups[label].append(key)
    return {g: sorted(v) for g, v in sorted(groups.items(), key=lambda kv: kv[0])}


def is_power_of_two(n: int) -> bool:
# Returns True if n is a power of two (n > 0).
    return (n & (n - 1) == 0) and n != 0


def make_output_dirs(base_path: str) -> tuple[str, Optional[str]]:
# Creates/returns the output and optional backup directories for a given base path:

    base_path = os.path.abspath(base_path or ".")
    out_name = (DEST_FOLDER_NAME or "").strip()
    out_dir = os.path.join(base_path, out_name) if out_name else base_path
    os.makedirs(out_dir, exist_ok=True)

    bak_dir = None
    bak_name = (BACKUP_FOLDER_NAME or "").strip()
    if bak_name:
        bak_dir = os.path.join(base_path, bak_name)
        os.makedirs(bak_dir, exist_ok=True)

    return out_dir, bak_dir


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

    only_type: str = rf"{separator}{re.escape(type_suffix)}$"
    if re.search(only_type, name_lower):
        return only_type
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


def validate_safe_folder_name(raw_name: Optional[str]) -> str:
# Validates that the custom folder name doesn't include unsupported characters.

    name: str = (raw_name or "")
    if name.strip() == "":
        return ""

    if any(ch in name for ch in '\\/:*?"<>|'):
        log(f"Aborted: invalid folder name '{raw_name}'. It cannot contain \\ / : * ? \" < > |", "error")
        # Prints error.
        raise SystemExit(1)
    return name.strip()