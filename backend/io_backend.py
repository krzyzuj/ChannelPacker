
""" Input processing backend: unifies system CLI and Unreal Engine, so the main channel_packer logic is platform-agnostic. """
#  It is now split into two separate packages but still allows the channel_packer function to be used interchangeably between them.

import os
import re
import shutil
from typing import Dict, Iterable, List, Optional, Set, Tuple
from collections import defaultdict
from dataclasses import dataclass, field

from backend.image_lib import (ImageObj, save_image as save_image_file)

from settings import (CUSTOM_FOLDER_NAME, BACKUP_FOLDER_NAME, ALLOWED_FILE_TYPES, FILE_TYPE, DELETE_USED)

from utils import log


@dataclass
class CPContext:
    work_dir: str = None # Absolute path for a temporary folder.
    selection_paths: Dict[str, str] = field(default_factory=dict) # Maps input keys (rel path on Windows) to their absolute file path.
    export_ext: str = "" # Validated file extension set in config.




#                                     === Channel Packer core interface ===

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


def validate_export_ext_ctx(ctx: "CPContext" = None) -> None:
# Validates and sets in context extension input by the user in config.
# Sets just extension typ, without the dot.

    allowed: set[str] = set(ALLOWED_FILE_TYPES)
    ext: str = (FILE_TYPE or "").strip().lower().lstrip(".")

    if not ext or ext not in allowed:
        pretty_allowed = ", ".join(sorted(allowed))
        log(f"Aborted: Invalid FILE_TYPE '{FILE_TYPE}'. Supported: {pretty_allowed}", "error")
        raise SystemExit(1)

    if ctx is not None:
        ctx.export_ext = ext
    return


def split_by_parent(ctx: "CPContext") -> Dict[str, List[str]]:
# Groups absolute paths from ctx.selection_paths values by their parent directory relative to ctx.work_dir.
# Returns a sorted rel_parent: [file names] map.

    getattr(ctx, "work_dir", None)
    files_abs: List[str] = [v for v in getattr(ctx, "selection_paths", {}).values() if v]
    root: str = os.path.abspath(ctx.work_dir)
    groups: Dict[str, List[str]] = defaultdict(list)

    for ap in files_abs:
        if not os.path.isabs(ap):
            ap = os.path.abspath(ap)
        try:
            rel = os.path.relpath(ap, root)
        except Exception:
            continue
        rel = rel.replace("\\", "/")
        parent = os.path.dirname(rel)
        key = parent if parent else "."
        name = os.path.basename(rel)
        groups[key].append(name)

    return {k: sorted(v) for k, v in sorted(groups.items(), key=lambda kv: kv[0])}


def make_output_dirs(base_path: str) -> tuple[str, Optional[str]]:
# Creates/returns the output and optional backup directories for a given base path:
# ctx used only in the Unreal version.

    base_path = os.path.abspath(base_path or ".")
    out_name = (CUSTOM_FOLDER_NAME or "").strip()
    out_dir = os.path.join(base_path, out_name) if out_name else base_path
    os.makedirs(out_dir, exist_ok=True)

    bak_dir = None
    bak_name = (BACKUP_FOLDER_NAME or "").strip()
    if bak_name:
        bak_dir = os.path.join(base_path, bak_name)
        os.makedirs(bak_dir, exist_ok=True)

    return out_dir, bak_dir


def list_initial_files(input_folder: str, ctx: "CPContext" = None, recursive: bool = False,) -> list[str]:
# Lists candidate files from input_folder. On Windows returns paths relative to work_dir - where are located the input files.
# Recursive is not used on Windows.

    if not input_folder:
        if ctx is not None:
            ctx.selection_paths = {}
        return []

    base = os.path.abspath(input_folder)
    if ctx is not None:
        ctx.work_dir = base  # setting CTX work dir

    files: list[str] = []

    blocked_dirs = {
        n.strip() for n in (CUSTOM_FOLDER_NAME, BACKUP_FOLDER_NAME)
        if n and n.strip()
    }

    if recursive:
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in blocked_dirs]
            for nm in names:
                if nm.lower().endswith(ALLOWED_FILE_TYPES):
                    abs_p = os.path.join(root, nm)
                    rel_p = os.path.relpath(abs_p, base).replace("\\", "/")
                    files.append(rel_p)
    else:
        for nm in os.listdir(base):
            if nm.lower().endswith(ALLOWED_FILE_TYPES):
                abs_p = os.path.join(base, nm)
                if os.path.isfile(abs_p):
                    files.append(nm)

    files.sort()
    ctx.selection_paths = {rel: "" for rel in files}
    # Collects relative file paths as keys in the dict, prepare_workspace fills in values as absolute paths.
    # For compatibility reasons with Engine paths - engine assets need exporting first during prepare_workspace.
    return files


def prepare_workspace(_assets_keys_unused: List[str], ctx: "CPContext" = None) -> None:
# Resolves each relative path from ctx.selection_paths (key) to an absolute path under ctx.work_dir (values).
# Removes entries whose path contains CUSTOM_FOLDER_NAME or BACKUP_FOLDER_NAME to avoid reprocessing output/backup folders.

    if not ctx.work_dir:
        return

    work_dir: str = os.path.abspath(ctx.work_dir or ".")
    blocked_dirs = {
        n.strip() for n in (CUSTOM_FOLDER_NAME, BACKUP_FOLDER_NAME)
        if n and n.strip()
    }

    for rel in list(ctx.selection_paths.keys()):
        parts = [seg for seg in rel.split("/") if seg]

        if any(seg in blocked_dirs for seg in parts):
            ctx.selection_paths.pop(rel, None)
            continue
        # Skips to avoid reprocessing output/backup folders.

        abs_p = os.path.abspath(os.path.join(work_dir, rel)).replace("\\", "/")
        ctx.selection_paths[rel] = abs_p
    return


def save_image(img: ImageObj, out_dir: str, filename: str, mode_name: str, ctx: Optional["CPContext"]) -> None:
# On Windows just saves to out_dir.
# Mode_name and ctx used only in the Unreal version.
    os.makedirs(out_dir, exist_ok=True)
    ext = (getattr(ctx, "export_ext", "") or "png").lstrip(".").lower() if ctx else "png"
    out_path = os.path.join(out_dir, f"{filename}.{ext}")
    try:
        save_image_file(img, out_path)
        return True
    except Exception:
        return False


def move_used_map(src_path: str, bak_dir: Optional[str], ctx: Optional["CPContext"]) -> None:
 # Moves maps used to generate the channel-packed texture to the backup folder if specified in the config.
 # ctx used only in the Unreal version.


    if not bak_dir or DELETE_USED:
        return
    try:
        if not src_path or not os.path.exists(src_path):
            return

        os.makedirs(bak_dir, exist_ok=True)

        base = os.path.basename(src_path)
        dest = os.path.join(bak_dir, base)


        if os.path.exists(dest):
            name, ext = os.path.splitext(base)
            i = 2
            while True:
                alt = os.path.join(bak_dir, f"{name}_{i}{ext}")
                if not os.path.exists(alt):
                    dest = alt
                    break
                i += 1
        # Adds suffixes in case same named files end up in the directory.

        shutil.move(src_path, dest)

    except Exception as e:
        log(f"Warning: failed to move '{src_path}' → '{bak_dir}': {e}", "warn")


def cleanup(ctx: "CPContext") -> None:
# On Windows only deletes the files used for the generation if set in config.

    if not DELETE_USED:
        return
    sel_map = getattr(ctx, "selection_paths", {}) or {}

    for ap in sel_map.values():
        if not ap:
            continue
        try:
            if os.path.isfile(ap):
                os.remove(ap)
        except FileNotFoundError:
            pass
        except PermissionError as e:
            log(f"No permission to remove '{ap}': {e}", "warn")
        except OSError as e:
            log(f"Failed to remove '{ap}': {e}", "warn")
























































