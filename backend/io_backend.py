
""" Input processing backend: unifies system CLI and Unreal Engine, so the main channel_packer logic is platform-agnostic. """
#  It is now split into two separate packages but still allows the channel_packer function to be used interchangeably between them.

import os

import shutil
from typing import Dict, List, Set, Optional
from collections import defaultdict
from dataclasses import dataclass, field

from backend.image_lib import (ImageObject, save_image as save_image_file)

from settings import (ALLOWED_FILE_TYPES, BACKUP_FOLDER_NAME, DELETE_USED, TARGET_FOLDER_NAME, EXR_SRGB_CURVE, FILE_TYPE, SHOW_DETAILS)
from utils import (check_exr_libraries, convert_exr_to_image, log)


@dataclass
class ConvertedEXRImage:
    source_exr_path: str # Path to the source .exr file.
    texture_set_name: Optional[str] = None # Texture set name, added later.
    texture_map_type: Optional[str] = None # Texture type name.

@dataclass
class CPContext:
    work_directory: str = None # Absolute path for a temporary folder.
    selection_paths_map: Dict[str, str] = field(default_factory=dict) # Maps paths relative to the root directory to their absolute file paths.
    export_extension: str = "" # Validated file extension set in config.
    textures_converted_from_raw: Dict[str, ConvertedEXRImage] = field(default_factory=dict)  # Collection of temporary converted .exr files for processing in the main module, their raw_source path, texture set name and its texture type.


RAW_SOURCE_TYPES: tuple[str] = (".exr",)  # Makes .exr file discoverable by the script additionally to the "regular" file format types.




#                                     === Channel Packer core interface ===




def context_validate_export_extension(context: "CPContext" = None) -> None:
# Validates and sets in context extension input by the user in config.
# Sets the extension type, without the dot.

    allowed_file_types: set[str] = set(ALLOWED_FILE_TYPES)
    typed_extension: str = (FILE_TYPE or "").strip().lower().lstrip(".")

    if typed_extension == "jpg":
        file_extension: str = "jpeg"
    else:
        file_extension: str = typed_extension

    if not file_extension or file_extension not in allowed_file_types:
        sorted_allowed_file_types = ", ".join(sorted(allowed_file_types))
        log(f"Aborted: Invalid FILE_TYPE '{FILE_TYPE}'. Supported: {sorted_allowed_file_types}", "error")
        raise SystemExit(1)

    if context is not None:
        context.export_extension = file_extension
    return


def split_by_parent(context: "CPContext") -> Dict[str, List[str]]:
# Groups absolute paths from context.selection_paths values by their parent directory relative to contex.work_dir.
# Returns a sorted rel_parent: [file names] map.

    file_absolute_paths: List[str] = [absolute_path for absolute_path in context.selection_paths_map.values() if absolute_path and absolute_path.strip()]
    root_directory: str = os.path.abspath(context.work_directory)
    filenames_by_parent_folder: Dict[str, List[str]] = defaultdict(list)

    for file_absolute_path in file_absolute_paths:
        if not os.path.isabs(file_absolute_path):
            file_absolute_path = os.path.abspath(file_absolute_path)
        try:
            relative_path: str = os.path.relpath(file_absolute_path, root_directory)
        except Exception:
            continue
        relative_path: str = relative_path.replace("\\", "/")
        parent_directory_ = os.path.dirname(relative_path.replace("\\", "/")) or "."
        filename_ = os.path.basename(relative_path)
        filenames_by_parent_folder[parent_directory_].append(filename_)

    return {parent_directory: sorted(filename) for parent_directory, filename in sorted(filenames_by_parent_folder.items())}


def list_initial_files(input_folder: str, context: "CPContext" = None, recursive: bool = False, ) -> list[str]:
# Lists candidate files from input_folder. On Windows returns paths relative to work_dir - where are located the input files.
# Recursive is not used on Windows.

    source_file_types = ALLOWED_FILE_TYPES + RAW_SOURCE_TYPES


    if not input_folder:
        if context is not None:
            context.selection_paths_map = {}
        return []

    root_directory = os.path.abspath(input_folder)
    if context is not None:
        context.work_directory = root_directory  # setting context work dir

    relative_paths: list[str] = []

    blocked_directories = {
        directory_name.strip() for directory_name in (TARGET_FOLDER_NAME, BACKUP_FOLDER_NAME)
        if directory_name and directory_name.strip()
    }

    if recursive:
        for root, subdirectories, filenames in os.walk(root_directory):
            subdirectories[:] = [subdirectory for subdirectory in subdirectories if subdirectory not in blocked_directories]
            for filename in filenames:
                if filename.lower().endswith(source_file_types):
                    absolute_path = os.path.join(root, filename)
                    relative_path = os.path.relpath(absolute_path, root_directory).replace("\\", "/")
                    relative_paths.append(relative_path)
    else:
        for filename in os.listdir(root_directory):
            if filename.lower().endswith(source_file_types):
                absolute_path = os.path.join(root_directory, filename)
                if os.path.isfile(absolute_path):
                    relative_paths.append(filename)

    relative_paths.sort()
    context.selection_paths_map = {relative_path_: "" for relative_path_ in relative_paths}
    # Collects relative file paths as keys in the dict, prepare_workspace fills in values as absolute paths.
    # For compatibility reasons with Engine paths - engine assets need exporting first during prepare_workspace.
    return relative_paths


def prepare_workspace(_assets_keys_unused: List[str], context: "CPContext" = None) -> None:
# Resolves each relative path from ctx.selection_paths (keys) to an absolute path under ctx.work_dir (values).
# Removes entries whose path contains DEST_FOLDER_NAME or BACKUP_FOLDER_NAME to avoid reprocessing output/backup folders.

    if not context.work_directory:
        return

    work_directory: str = os.path.abspath(context.work_directory or ".")
    blocked_folder_names = {folder_name.strip() for folder_name in (TARGET_FOLDER_NAME, BACKUP_FOLDER_NAME) if folder_name and folder_name.strip()}

    for relative_path in list(context.selection_paths_map.keys()):
        path_segments = [path_segment for path_segment in relative_path.split("/") if path_segment]

        if any(path_segment in blocked_folder_names for path_segment in path_segments):
            context.selection_paths_map.pop(relative_path, None)
            continue
        # Skips to avoid reprocessing output/backup folders.

        absolute_path = os.path.abspath(os.path.join(work_directory, relative_path)).replace("\\", "/")
        source_file_extension = os.path.splitext(absolute_path)[1].lower()


        if source_file_extension in RAW_SOURCE_TYPES:
            if check_exr_libraries():
                output_path = convert_exr_to_image(absolute_path, file_extension= context.export_extension, delete_source_files= False, srgb_transform = EXR_SRGB_CURVE)
                if output_path:
                    context.selection_paths_map[relative_path] = output_path
                    context.textures_converted_from_raw[output_path] = ConvertedEXRImage(source_exr_path=absolute_path) # Mapping the temporary converted files, for logs and to be later deleted during the cleanup.
                else:
                    context.selection_paths_map.pop(relative_path, None)
                    log(f"Skipping '{absolute_path}': cannot convert EXR to {context.export_extension}.", "error")
            else:
                context.selection_paths_map.pop(relative_path, None)
                log(f"Skipping '{absolute_path}': EXR runtime missing (OpenEXR/NumPy).", "warn")
            continue
        # Pre-processing the .exr files.

        context.selection_paths_map[relative_path] = absolute_path


def save_image(image: ImageObject, output_directory: str, filename: str, packing_mode_name: str, context: Optional["CPContext"]) -> None:
# On Windows just saves to out_dir.
# Packing_mode_name and context are used only in the Unreal version.

    os.makedirs(output_directory, exist_ok=True)
    output_extension = (getattr(context, "export_ext", "") or "png").lstrip(".").lower() if context else "png"
    output_path = os.path.join(output_directory, f"{filename}.{output_extension}")
    try:
        save_image_file(image, output_path)
        return True
    except Exception:
        return False


def move_used_map(source_path: str, backup_directory: Optional[str], context: Optional["CPContext"]) -> None:
 # Moves maps used to generate the channel-packed texture to the backup folder if specified in the config.
 # Context used only in the Unreal version.


    if not backup_directory or DELETE_USED:
        return
    try:
        if not source_path or not os.path.exists(source_path):
            return

        os.makedirs(backup_directory, exist_ok=True)

        filename = os.path.basename(source_path)
        target_path = os.path.join(backup_directory, filename)


        if os.path.exists(target_path):
            filename, file_extension = os.path.splitext(filename)
            i = 2
            while True:
                alternative_path = os.path.join(backup_directory, f"{filename}_{i}{file_extension}")
                if not os.path.exists(alternative_path):
                    target_path = alternative_path
                    break
                i += 1
        # Adds suffixes in case same named files end up in the directory.

        shutil.move(source_path, target_path)

    except Exception as error:
        log(f"Warning: failed to move '{source_path}' to '{backup_directory}': {error}", "warn")


def cleanup(context: "CPContext") -> None:
# On Windows only deletes the files used for the generation if set in config.

# Deleting temporary files from .exr conversion.:
    temporary_paths: set = set(context.textures_converted_from_raw.keys())
    for path in temporary_paths:
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except PermissionError as error:
                log(f"No permission to remove temp '{path}': {error}", "warn")
            except OSError as error:
                log(f"Failed to remove temp '{path}': {error}", "warn")

    if not DELETE_USED:
        return


# Deleting regular images used for packing:
    selection_paths: Dict[str, str] = context.selection_paths_map
    for absolute_path in selection_paths.values():
        if not absolute_path:
            continue
        try:
            if os.path.isfile(absolute_path):
                os.remove(absolute_path)
        except FileNotFoundError:
            pass
        except PermissionError as error:
            log(f"No permission to remove '{absolute_path}': {error}", "warn")
        except OSError as error:
            log(f"Failed to remove '{absolute_path}': {error}", "warn")

# Deleting original .exr files used for packing:
    raw_source_paths: Set[ConvertedEXRImage] = {path for path in context.textures_converted_from_raw.values() if path}
    for raw_source_path in raw_source_paths:
        if os.path.isfile(raw_source_path):
            try: os.remove(raw_source_path)
            except FileNotFoundError: pass
            except PermissionError as error: log(f"No permission to remove '{raw_source_path}': {error}", "warn")
            except OSError as error: log(f"Failed to remove '{raw_source_path}': {error}", "warn")