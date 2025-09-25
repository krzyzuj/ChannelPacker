
""" Input processing backend: unifies system CLI and Unreal Engine, so the main channel_packer logic is platform-agnostic. """
#  It is now split into two separate packages but still allows the channel_packer function to be used interchangeably between them.

import os

import shutil
from typing import Dict, List, Set, Optional
from collections import defaultdict
from dataclasses import dataclass, field

from backend.image_lib import (ImageObj, save_image as save_image_file)

from settings import (ALLOWED_FILE_TYPES, BACKUP_FOLDER_NAME, DELETE_USED, DEST_FOLDER_NAME, EXR_SRGB_CURVE, FILE_TYPE, SHOW_DETAILS)
from utils import (check_exr_libraries, convert_exr_to_image, log)


@dataclass
class ConvertedEntry:
    src_exr: str # Path to the source .exr file.
    set_key: Optional[str] = None # Texture set name, added later.
    map_type: Optional[str] = None # Texture type name.

@dataclass
class CPContext:
    work_dir: str = None # Absolute path for a temporary folder.
    selection_paths: Dict[str, str] = field(default_factory=dict) # Maps input keys (rel path on Windows) to their absolute file path.
    export_ext: str = "" # Validated file extension set in config.
    converted_from_raw: Dict[str, ConvertedEntry] = field(default_factory=dict)  # Collection of temporary converted .exr files, for processing in the main module, their raw_source path, and their texture set.


RAW_SOURCE_TYPES: tuple[str] = (".exr",)  # Makes .exr file discoverable by the script additionally to the "regular" file format types.




#                                     === Channel Packer core interface ===




def validate_export_ext_ctx(ctx: "CPContext" = None) -> None:
# Validates and sets in context extension input by the user in config.
# Sets the extension type, without the dot.

    allowed: set[str] = set(ALLOWED_FILE_TYPES)
    raw_ext: str = (FILE_TYPE or "").strip().lower().lstrip(".")

    if raw_ext == "jpg":
        ext: str = "jpeg"
    else:
        ext: str = raw_ext

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


def list_initial_files(input_folder: str, ctx: "CPContext" = None, recursive: bool = False,) -> list[str]:
# Lists candidate files from input_folder. On Windows returns paths relative to work_dir - where are located the input files.
# Recursive is not used on Windows.

    source_file_types = ALLOWED_FILE_TYPES + RAW_SOURCE_TYPES


    if not input_folder:
        if ctx is not None:
            ctx.selection_paths = {}
        return []

    base = os.path.abspath(input_folder)
    if ctx is not None:
        ctx.work_dir = base  # setting CTX work dir

    files: list[str] = []

    blocked_dirs = {
        n.strip() for n in (DEST_FOLDER_NAME, BACKUP_FOLDER_NAME)
        if n and n.strip()
    }

    if recursive:
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in blocked_dirs]
            for nm in names:
                if nm.lower().endswith(source_file_types):
                    abs_p = os.path.join(root, nm)
                    rel_p = os.path.relpath(abs_p, base).replace("\\", "/")
                    files.append(rel_p)
    else:
        for nm in os.listdir(base):
            if nm.lower().endswith(source_file_types):
                abs_p = os.path.join(base, nm)
                if os.path.isfile(abs_p):
                    files.append(nm)

    files.sort()
    ctx.selection_paths = {rel: "" for rel in files}
    # Collects relative file paths as keys in the dict, prepare_workspace fills in values as absolute paths.
    # For compatibility reasons with Engine paths - engine assets need exporting first during prepare_workspace.
    return files


def prepare_workspace(_assets_keys_unused: List[str], ctx: "CPContext" = None) -> None:
# Resolves each relative path from ctx.selection_paths (keys) to an absolute path under ctx.work_dir (values).
# Removes entries whose path contains DEST_FOLDER_NAME or BACKUP_FOLDER_NAME to avoid reprocessing output/backup folders.

    if not ctx.work_dir:
        return

    work_dir: str = os.path.abspath(ctx.work_dir or ".")
    blocked_dirs = {n.strip() for n in (DEST_FOLDER_NAME, BACKUP_FOLDER_NAME)if n and n.strip()}

    for rel in list(ctx.selection_paths.keys()):
        parts = [seg for seg in rel.split("/") if seg]

        if any(seg in blocked_dirs for seg in parts):
            ctx.selection_paths.pop(rel, None)
            continue
        # Skips to avoid reprocessing output/backup folders.

        abs_path = os.path.abspath(os.path.join(work_dir, rel)).replace("\\", "/")
        ext = os.path.splitext(abs_path)[1].lower()


        if ext in RAW_SOURCE_TYPES:
            if check_exr_libraries():
                out_path = convert_exr_to_image(abs_path, ext = ctx.export_ext, delete_src = False, srgb_transform = EXR_SRGB_CURVE)
                if out_path:
                    ctx.selection_paths[rel] = out_path
                    ctx.converted_from_raw[out_path] = ConvertedEntry(src_exr=abs_path) # Mapping the temporary converted files, for logs and to be later deleted during the cleanup.
                else:
                    ctx.selection_paths.pop(rel, None)
                    log(f"Skipping '{abs_path}': cannot convert EXR to {ctx.export_ext}.", "error")
            else:
                ctx.selection_paths.pop(rel, None)
                log(f"Skipping '{abs_path}': EXR runtime missing (OpenEXR/NumPy).", "warn")
            continue
        # Pre-processing the .exr files.

        ctx.selection_paths[rel] = abs_path


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

# Deleting temporary files from .exr conversion.:
    temp_paths: set = set(ctx.converted_from_raw.keys())
    for p in temp_paths:
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except PermissionError as e:
                log(f"No permission to remove temp '{p}': {e}", "warn")
            except OSError as e:
                log(f"Failed to remove temp '{p}': {e}", "warn")

    if not DELETE_USED:
        return


# Deleting regular images used for packing:
    sel_map: Dict[str, str] = getattr(ctx, "selection_paths", {}) or {}
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

# Deleting original .exr files used for packing:
    raw_sources: Set[str] = {p for p in ctx.converted_from_raw.values() if p}
    for raw in raw_sources:
        if os.path.isfile(raw):
            try: os.remove(raw)
            except FileNotFoundError: pass
            except PermissionError as e: log(f"No permission to remove '{raw}': {e}", "warn")
            except OSError as e: log(f"Failed to remove '{raw}': {e}", "warn")






















































