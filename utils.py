
""" Texture utilities. Separate module to keep compatibility Channel Packer version-agnostic. """

import os
import re
from typing import Dict, Iterable, List, Optional, Set, Tuple
from collections import defaultdict

from settings import (BACKUP_FOLDER_NAME, DEST_FOLDER_NAME, SIZE_SUFFIXES)

from backend.texture_classes  import (TextureMapData, MapNameAndRes)

from backend.image_lib import close_image


LOG_TYPES: str = ["info", "warn", "error", "skip", "complete"]
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


def close_image_files(images: Iterable[Optional[object]]) -> None:
# # Safely closes all opened images even if there is an error during image processing.

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


def detect_size_suffix(name: str) -> str:
    # Detects size suffixes present in the map name,  e.g., "2K"

    tokens: List[str] = sorted([s.lower() for s in SIZE_SUFFIXES if s], key=len, reverse=True)
    # Normalizes tokens to lowercase and sorts by reverse length to avoid shorter tokens matching before longer ones.
    if not tokens:
        return ""
    pattern = r"(?:[\._\-])(" + "|".join(map(re.escape, tokens)) + r")$"
    # Tries to match suffix variances to the map name
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
    # type...size, size...type, ...type

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