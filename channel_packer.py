
""" Generates channel-packed textures from source maps according to the configuration. """

import os
import sys
import re
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple, Union, cast

from backend.image_lib import (ImageObj, close_image, get_bands, get_channel,
     get_mode, get_size, merge_channels, new_gray, open_image, resize, to_grayscale)

from backend.texture_classes import (ChannelMapping, MapNameAndRes, PackingMode, SetEntry,
     TextureMapCollection, TextureMapData, TextureNameInfo, TextureSet, ValidModeEntry)

from settings import (TextureTypeConfig, ALLOWED_FILE_TYPES, BACKUP_FOLDER_NAME,
     DEST_FOLDER_NAME, INPUT_FOLDER, PACKING_MODES, RESIZE_STRATEGY, SHOW_DETAILS, TEXTURE_CONFIG)

from utils import (check_texture_suffix_mismatch, close_image_files, detect_size_suffix,
     group_paths_by_folder, is_power_of_two, log, make_output_dirs, match_suffixes, resolution_to_suffix, validate_safe_folder_name)

from backend.io_backend import (CPContext, validate_export_ext_ctx, split_by_parent,
     list_initial_files, prepare_workspace, save_image, move_used_map, cleanup)


# Basic data structure bundling textures and their metadata into a texture set:
# raw_textures = {
#     "rock": TextureSet(
#         original_name="Rock",
#         available_maps={
#             "Albedo": [
#                 TextureMapData(
#                     path="textures/Rock_Albedo.png",
#                     resolution=(2048, 2048),
#                     suffix="_2K",
#                     filename="Rock_Albedo_2K.png",
#                 )
#             ],
#             "Normal": [
#                 TextureMapData(
#                     path="textures/Rock_Normal.png",
#                     resolution=(2048, 2048),
#                     suffix="_2K",
#                     filename="Rock_Normal_2K.png",
#                 )
#             ],
#         processed=True,
#         completed=True,


#                                           === Pipeline ===

def channel_packer(input_folder: Optional[str] = None) -> None:
# Prepares texture sets for generating final channel-packed texture.
# Using an external ctx keeps the main function backend-agnostic.

    ctx = CPContext() # Context object holding the runtime state for the currently processed files.
    start_time = time.time()
    created_any: bool = False



# Validating config and setting up the files:
    _validate_config(RESIZE_STRATEGY, ctx) # Validate base settings (export ext, resize strategy).
    valid_packing_modes: List[PackingMode] = _validate_packing_modes()  # Validates packing modes from the config, fixes channel mappings, and skips empty/invalid entries:


    pre_skipped_summary = _validate_and_setup_files(input_folder or "", ctx, valid_packing_modes) # Validates and collects the files via backend into the context for further processing.
    # Returns skipped sets that don't have enough required maps for logging purposes at the end.
    work_dir = ctx.work_dir  # Absolute working directory for processing the files.




# Grouping each subfolder to a separate package:
    groups: Dict[str, List[str]] = split_by_parent(ctx)  # Creates a dict that groups files by their parent directory relative to ROOT ("."), e.g., {'.': [...], {'A': [...], 'A/B': [...],}
    multiple_groups = len(groups) > 1
    processed_group_order: List[str] = [] # Record the processing order of groups so the final "Skipped" logs follow the same sequence.


    for relative_parent, files_in_group in groups.items():
        final_path: str = work_dir if relative_parent == "." else os.path.join(work_dir, relative_parent)
        final_path = os.path.abspath(final_path)
        out_dir, bak_dir = make_output_dirs(final_path)
    # Creates output and backup folders (if set) per texture set's folder.


        if multiple_groups:
            log_prefix = "[ROOT] " if relative_parent == "." else f"[{relative_parent}] "
        else:
            log_prefix = ""
        # Adds folder prefix for logging if source textures are in more than one folder.


# Collecting maps into texture sets for each folder:
        raw_textures: Dict[str, TextureSet] = _build_texture_sets(final_path, files_in_group, ctx=ctx) # Collecting maps into texture sets data.
        processed_group_order.append(relative_parent)

# Filtering modes to those with at least two required maps, then choosing the target resolution and logging any mismatches.
        for tex_set_name, tex_set in raw_textures.items():
            original_tex_set_name: str = tex_set.tex_set_name
            available_maps: TextureMapCollection = tex_set.available_maps

            valid_modes_with_maps: List[ValidModeEntry] = _get_valid_modes_for_set(original_tex_set_name, available_maps, valid_packing_modes)
            # Collects packing modes applicable to this set (requires at least two maps).

            target_resolution: Dict[str, Tuple[int, int]] = {}
            # Chosen target resolution for each mode; used to normalize map sizes and log mismatch.

            suffix_warnings_accum: List[MapNameAndRes] = []
            displayed_global_suffix_warning = False
            displayed_global_resolution_warning = False
            # Log buffers (per set).

            if not valid_modes_with_maps:
                tex_set.completed = True
                continue
            # Skips if there are less than 2 necessary maps for a given packing mode; the flag is used in logs.

            name_for_log = f"{log_prefix}{original_tex_set_name}" if log_prefix else original_tex_set_name
            log(f"\nProcessing: {name_for_log}", "info")
            # Prints info.

            invalid_mode_names_for_set: Set[str] = set() # Collects modes that do not meet any criteria, used to create final valid_modes_with_maps.
            invalid_dims_for_summary: Dict[str, Tuple[int, int]] = {}  # Invalid dimensions for textures that are not 2^n

            for entry in valid_modes_with_maps:
                maps_for_mode: TextureMapCollection = entry.maps_for_mode

                is_valid, res = _check_textures_and_pick_target_resolution(
                    maps_for_mode=maps_for_mode,
                    strategy=RESIZE_STRATEGY,
                    mode_name=entry.mode["mode_name"],
                )
                # Finds listed textures resolutions and checks if textures have mip friendly power-of-two (2^n) res.

                if not is_valid:
                    invalid_mode_names_for_set.add(entry.mode["mode_name"])
                    invalid_dims_for_summary[entry.mode["mode_name"]] = res
                    continue
                # # Returns mode names whose textures either have an incorrect resolution or are corrupted (reported as 0×0).

                target_size = res
                textures_to_scale: List[MapNameAndRes] = _list_textures_to_scale(maps_for_mode, target_size) # Lists all textures with mismatched resolutions that need to be scaled before channel packing.

                displayed_global_resolution_warning = _print_warnings(
                    textures_to_scale,
                    displayed_global_resolution_warning,
                    warning_type="resolution",
                    target_resolution=target_size,
                )
                # Prints warning if textures have different resolution within a single texture set.

                suffix_warnings_accum.extend(_check_suffix_warnings_for_set(maps_for_mode))
                # Lists all files whose size suffix (if available) does not match the actual resolution.

                target_resolution[entry.mode["mode_name"]] = target_size
                # Sets a target resolution for a given packing mode.


 # Finalizing modes for a set and printing warnings before generation:
            modes_ready = [
                e for e in valid_modes_with_maps
                if e.mode["mode_name"] not in invalid_mode_names_for_set
            ]
            modes_invalid = [
                e for e in valid_modes_with_maps
                if e.mode["mode_name"] in invalid_mode_names_for_set
            ]

            if not modes_ready:
                tex_set.completed = True
                # Case when all the modes have invalid textures resolution.
                # Marks them as completed, not to be displayed in logs as skipped due to not having required maps, even though no channel_packed textures were generated.
                if modes_invalid:
                    tex_set.processed = True
                valid_modes_with_maps = []
                # Skips mode if it doesn't have any valid modes, so the "Skipped: _mode" isn't generated for them,
                # Instead all texture sets that have no valid packing modes go into the general"skipped"" summary at the end of the logs.
            else:
                valid_modes_with_maps = modes_ready

            displayed_global_suffix_warning = _print_warnings(
                suffix_warnings_accum,
                displayed_global_suffix_warning,
                warning_type="suffix",
            )
            # Prints warning if size suffixes in the file name (if present) do not match the actual texture size


            for entry in valid_modes_with_maps:
                channel_mapping: ChannelMapping = entry.mode.get("channels", {})
                missing_maps = _list_missing_maps_for_channel_mapping(channel_mapping, entry.maps_for_mode)

                _print_warnings(
                    missing_maps,
                    False,  # Prints only once per mode
                    warning_type="missing_maps",
                    mode_name=entry.mode.get("mode_name", ""),
                )
             # Prints warning if size suffixes in the file name (if present) do not match the actual texture size.



# Generating channel packed texture:
            for entry in valid_modes_with_maps:
                filename = _generate_channel_packed_texture(
                    entry,
                    target_resolution,
                    out_dir,
                    bak_dir,
                    ctx
                )
                if filename:
                    tex_set.processed = True
                    tex_set.completed = True
                    created_any = True




            _summarize_mode_results(
                original_tex_set_name,
                valid_packing_modes,
                valid_modes_with_maps,
                target_resolution,
                ctx=ctx,
                invalid_modes=invalid_mode_names_for_set,
                invalid_mode_dims=invalid_dims_for_summary,
                log_prefix=log_prefix
            )



# Printing summary logs for all the processed folders, and cleaning up temporary files:

    if pre_skipped_summary:
        log("", "info")  # Visual separator
        groups_in_order: List[str] = [g for g in processed_group_order if g in pre_skipped_summary] + [g for g in pre_skipped_summary.keys() if g not in processed_group_order]
        # Lists texture sets and their available maps skipped due to missing required maps + textures sets and their maps from folders that produced no successfully packed textures.

        for relative_parent in groups_in_order:
            groups_map = pre_skipped_summary[relative_parent]
            prefix = f"[{'ROOT' if relative_parent in ('.', '') else relative_parent}] " if multiple_groups else "" # Adds folder prefix only when run with multiple folders. Also displays "ROOT" for the root folder.
            for display_name, files in groups_map.items():
                details = f" (files: {', '.join(files)})" if SHOW_DETAILS and files else ""
                log(f"Info: {prefix}Skipped '{display_name}' set – missing required maps{details}", "info")
    # Prints skipped only for texture sets that had not enough maps for any of the set channel packing modes.



    log("", "info")  # Visual separator
    log("All processing done.", "complete")


    if DEST_FOLDER_NAME.strip() and created_any:
        if multiple_groups:
            log(f"Packed maps saved to '{DEST_FOLDER_NAME}' subfolder(s) inside processed folders.", "info")
        else:
            only_group = next(iter(groups))
            final_path = work_dir if only_group in (".", "") else os.path.join(work_dir, only_group) # In case the only processed textures were in the subfolder.
            abs_out = os.path.abspath(os.path.join(final_path, DEST_FOLDER_NAME))
            log(f"Packed maps saved to: {abs_out}", "info")
        # For a single group run prints the absolute output path for the only processed folder.

    if BACKUP_FOLDER_NAME.strip() and created_any:
        if multiple_groups:
            log(f"Source maps moved to backup folder '{BACKUP_FOLDER_NAME}' inside processed folders.", "info")
        else:
            only_group = next(iter(groups))
            final_path = work_dir if only_group in (".", "") else os.path.join(work_dir, only_group) # In case the only processed textures were in the subfolder.
            abs_bak = os.path.abspath(os.path.join(final_path, BACKUP_FOLDER_NAME))
            log(f"Source maps moved to: {abs_bak}", "info")
        # For a single group run prints the absolute output path for the only processed folder.

    # Displays summary logs for single as well as multiple folders.


    cleanup(ctx)
    # Deletes UE temporary files or used files on Windows.


    if SHOW_DETAILS:
        elapsed = time.time() - start_time
        log(f"Execution time: {elapsed:.2f} seconds", "info")
        # Prints info.




#                                       === Validation & Setup ===

def _validate_config(resize_strategy: str, ctx: Optional[CPContext] = None) -> None:
# Runs initial validation for the config.
# Later on the config is checked for packing mode validity when running valid_modes for each packing mode.

    bak: str = validate_safe_folder_name(BACKUP_FOLDER_NAME)
    custom: str = validate_safe_folder_name(DEST_FOLDER_NAME)
    # Checks if folder names don't contain unsupported characters.

    validate_export_ext_ctx(ctx) # Validates the selected output extension and stores it in context
    export_ext: str = ctx.export_ext.lower()

    if not export_ext:
        log("Aborted: could not resolve output extension.", "error")
        raise SystemExit(1)
    # Validates and stores chosen export extension.

    uses_alpha = any((mode.get("channels") or {}).get("A") for mode in PACKING_MODES)

    if uses_alpha and export_ext in ("jpg", "jpeg", "jfif"):
        log(
            f"Aborted: Texture is mapped to the Alpha channel, but selected file type '{export_ext}' does not support alpha. Change FILE_TYPE to 'png' or 'tga' and retry.","error",)
        raise SystemExit(1)
    # Raises an error when texture is mapped to alpha, but output file type doesn't support it.


    rs = (resize_strategy or "").lower()
    if rs not in ("up", "down"):
        log(f"Warning: Unknown RESIZE_STRATEGY '{resize_strategy}'. Defaulting to 'down'.", "warn")
        # Prints Warning.
    # Checks for valid resize strategy in settings.
    return


def _validate_and_setup_files(input_folder: str, ctx: CPContext, valid_packing_modes: Optional[List["PackingMode"]] = None, ) -> Dict[str, Dict[str, List[str]]]:
# Prepares the files for further processing. Takes only the files that are actually used for the generation of the channel-packed maps.
# For Unreal Engine it exports files from the Content Browser to the temporary folder.
# For summary in logs, it returns texture sets that didn't have any texture maps needed for any of the packing modes.


    effective_input = input_folder or "" # Allows overriding an input path set in config when run from the system's CLI.
    initial_files: List[str] = list_initial_files(effective_input, ctx)

    if not initial_files:
        valid_output_exts = ", ".join((e if str(e).startswith(".") else f".{e}") for e in ALLOWED_FILE_TYPES)
        log(f"Aborted: No input files matching {valid_output_exts} in: {input_folder}", "error")
        raise SystemExit(1)


    pre_skipped_summary: Dict[str, Dict[str, List[str]]] = {} # Texture sets that didn't have any texture maps needed for any of the packing modes.

    if valid_packing_modes:
        pre_skipped_summary = _preselect_required_textures(valid_packing_modes, ctx)
    # Limits the file selection to textures required by the packing modes.


    prepare_workspace(list(ctx.selection_paths.keys()), ctx)
    # When run from the Engine, exports textures to a temporary workspace.
    # Resolves and fills absolute disk paths for each relative/package-path key.

    work_dir = ctx.work_dir
    if not (work_dir and os.path.isdir(work_dir)):
        log(f"Aborted: Working directory does not exist: {work_dir}", "error")
        raise SystemExit(1)
    return pre_skipped_summary


def _validate_packing_modes() -> List[PackingMode]:
    # Checks whether channels are mapped properly in the config; if not, tries to fix them or aborts execution.
    # If an RGB texture is mapped to a channel without an explicit component, defaults to the destination channel (e.g., R: Normal > R: Normal_R).
    # Removes unnecessary channel suffixes from grayscale maps (e.g., Height_R).
    # Returns PACKING_MODES with fixes applied.


    valid_modes: list[PackingMode] = []
    for mode in PACKING_MODES:
        mode_name: str = mode.get("mode_name", "").strip()
        if not mode_name:
            continue
        # Considers a packing mode valid only if it has a mode name.

        channels: ChannelMapping = mode.get("channels", {})
        fixed_channels: dict[str, str] = {}  # In case a grayscale texture map has an unnecessary suffix (e.g., R).

        for ch in ("R", "G", "B", "A"):
            val: Optional[str] = channels.get(ch)

            if not val:
                if ch == "A":
                    continue
                    # Alpha may be empty.
                else:
                    log(f"PACKING_MODE '{mode_name}' is missing required channel '{ch}'", "error")
                    # Prints error.
                    sys.exit(1)
            # Allows missing channel mapping only for Alpha; otherwise the script stops.

            match: Optional[re.Match[str]] = re.match(r"([a-z0-9]+)([._]?[rgb]?)$", val.strip(), re.IGNORECASE)
            # Extracts the map name and optional channel suffix using a regex.

            if not match:
                log(f"PACKING_MODE '{mode_name}' invalid syntax in channel '{ch}': {val}", "error")
                # Prints error.
                sys.exit(1)

            tex_name: str = match.group(1).lower()   # Receives map name.
            channel_suffix: str = match.group(2).lower() # Receives suffix or "".

            tex_config: Optional[TextureTypeConfig] = next(
                (cfg for key, cfg in TEXTURE_CONFIG.items() if key.lower() == tex_name),
                None
            )
            # Returns the first texture type (key and config) that matches the derived map name in TEXTURE_CONFIG.

            if tex_config is None:
                log(f"PACKING_MODE '{mode_name}' has unknown texture type set in {ch}: {val}", "error")
                # Prints error.
                sys.exit(1)

            map_type, _default_val = tex_config["default"]  # Fetches the map type (Grayscale or RGB).
            fixed_val: str = val or "" # Texture mapped to a channel with a proper component specified in the case of RGB textures and without any for the grayscale.

            if map_type.upper() == "RGB":
                if channel_suffix == "":
                    if ch in ("R", "G", "B"):
                        # If an RGB map lacks an explicit component, defaults to the destination channel (.r/.g/.b).
                        fixed_val: str = f"{tex_name}.{ch.lower()}"
                        if SHOW_DETAILS:
                            log(
                                f"PACKING_MODE '{mode_name}' channel '{ch}': "
                                f"'{val}' is RGB without channel, defaulting to '{fixed_val}'.",
                                "info"
                            )
                            # Prints info.
                    elif ch == "A":
                        log(
                            f"PACKING_MODE '{mode_name}' channel '{ch}' is assigned full RGB map '{val}' "
                            f"without explicit channel; Alpha must reference a single channel (e.g. '{val}.r') "
                            f"or a grayscale map, or be empty.",
                            "error"
                        )
                        sys.exit(1)
                        # Prints info.
                        # Cannot infer which component should map to Alpha; aborts.
                else:
                    fixed_val = val
                # Maps an RGB texture with a proper channel suffix.
            elif map_type.upper() == "G":
                fixed_val = tex_name if channel_suffix != "" else val
            # Removes unnecessary suffixes from grayscale maps.

            else:
                fixed_val = val
            fixed_channels[ch] = fixed_val

        # Replaces the channels dict with the fixed mapping (e.g., when a grayscale map has a channel suffix); a cast is required due to TypedDict.
        mode["channels"] = cast(ChannelMapping, fixed_channels) # Variable not typed due to TypedDict > Dict issue
        valid_modes.append(mode)
    return valid_modes




#                                         === Data Building ===

def _extract_tex_set_name(file_path_or_asset: str) -> Optional[TextureNameInfo]:
# Extracts info from the texture's name without opening the file - works with both files on a disc and Unreal's Content Browser paths.

    base: str = os.path.basename(file_path_or_asset)
    name, _ext = os.path.splitext(base)  # Gets the filename without extension
    name_lower: str = name.lower()
    size_suffix: Optional[str] = detect_size_suffix(name)


    for tex_type, config in TEXTURE_CONFIG.items(): # Derives texture type and size suffixes based on their aliases set in settings.
        tex_type = tex_type.lower()
        suffixes_lower = [s.lower() for s in config["suffixes"]]
        for type_suffix in suffixes_lower:
            pattern = match_suffixes(name_lower, type_suffix, size_suffix or None)
            if not pattern:
                continue
            regex = re.compile(pattern, flags=re.IGNORECASE)
            m = regex.search(name)
            if not m:
                continue
            # Tries to match extracted regex (type/size suffix permutation) to match with a file name.

            tex_set_name = name[:m.start()].rstrip("_-.") # Texture set name before the found suffix
            return (tex_set_name, tex_type, (size_suffix or "").lower(), name)
    return None


def _extract_tex_map_data(file_path: str) -> Optional[Tuple[int, int]]:
# Opens image to derive its actual resolution.
    try:
        image = open_image(file_path)
        resolution = get_size(image)
        close_image(image)
        return resolution
    except (OSError, ValueError) as e:
        log(f"Cannot open image file: {file_path} – {e}", "error")
        return None


def _preselect_required_textures(valid_packing_modes: List["PackingMode"], ctx: "CPContext", ) -> Dict[str, Dict[str, List[str]]]:
    # Narrows ctx.selection_paths to only the files actually required by the packing modes (keys only; absolute paths values are derived later in prepare_workspace).
    # Uses a unique texture_id: parent folder + texture set name to avoid file names collisions across folders.
    # On Windows the keys are relative file paths (e.g., "Subdir/T_Tex.png"); in Unreal the keys are package paths (e.g., "/Game/Textures/T_Tex").
    # Returns skipped sets for logging as: [parent folder]: tex set name (file names with original extensions).


# Basic data structure:
    # Sets:{
    #     "/Game/Textures/Wall:t_wall": {
    #         "display_name": "T_Wall",
    #         "types": {
    #             "albedo":    ["/Game/Textures/Wall/T_Wall_Albedo"],
    #             "normal":    ["/Game/Textures/Wall/T_Wall_Normal"],
    #             "roughness": ["/Game/Textures/Wall/T_Wall_Roughness"]},
    #         "untyped": []

    #     },
    #     "/Game/UI:logo": {
    #     "display_name": "Logo",
    #     "types": {},
    #     "untyped": ["/Game/UI/Logo"]}} # When a texture type can't be determined


    mode_suffixes = {_extract_mode_name(m).upper() for m in valid_packing_modes if _extract_mode_name(m)} # Get final suffixes for the created channel packed, to filter out maps that could already be there from previous script run.

# Collecting all the texture sets and their files into unique grouped sets:
    texture_sets: Dict[str, SetEntry] = {} # Collection of texture sets where key is the unique id derived from textures paths and set names. Contains display_name, recognized map types : lists of file paths, and untyped files.
    pre_skipped_sets: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list)) # Mapping of textures sets and its texture set, grouped by unique id, listing texture sets skipped due to not having rewired maps for any valid packaging mode.
    grouped = group_paths_by_folder(ctx.selection_paths.keys())  # Groups file paths by their parent folders relative to the root fodler.

    for group_folder, file_paths in grouped.items():
        for key in file_paths:

            asset_name: str = key.rsplit("/", 1)[-1]
            asset_name_no_ext: str = asset_name.rsplit(".", 1)[0]
            # Makes sure there is no extension at the end of the file name for names derived from both system path and Unreal Content Browser.


            size = detect_size_suffix(asset_name_no_ext)
            if size:
                base = asset_name_no_ext[:-(len(size) + 1)]
            else:
                base = asset_name_no_ext

            maybe_suffix = base.rsplit("_", 1)[-1].upper()
            if maybe_suffix in mode_suffixes:
                continue
            # Filters out files that have suffix matching of the currently channel packed textures, to omin files that could be from the previous script runs.


            extracted_info: Optional[Tuple[str, str, str, str]] = _extract_tex_set_name(asset_name)
            if extracted_info:
                tex_set_name, tex_type, _, _ = extracted_info
                texture_id = f"{group_folder}:{tex_set_name.lower()}"
                # Unique id built from the parent folder and set name; avoids collisions when identical set names exist in different directories.

                entry = texture_sets.get(texture_id)
                if entry is None: # Creates a texture set entry for every first texture from the same texture set.
                    entry = SetEntry(display_name=tex_set_name, types={}, untyped=[])
                    texture_sets[texture_id] = entry
                entry["types"].setdefault(tex_type.lower(), []).append(key) # Assigns a texture type, and its file path to a texture set.
            # Combines all the textures into the sets [types] - if the script was able to derive a texture set from the name, e.g., "Texture_AO.png".

            else:
                derived_asset_name = "_".join(asset_name_no_ext.split("_")[:-1]) if "_" in asset_name_no_ext else asset_name_no_ext
                tex_set_name = derived_asset_name or asset_name_no_ext
                texture_id = f"{group_folder}:{tex_set_name.lower()}"
                entry = texture_sets.get(texture_id)
                if entry is None:
                    entry = SetEntry(display_name=tex_set_name, types={}, untyped=[])
                    texture_sets[texture_id] = entry
                entry["untyped"].append(key)
            # List all remaining textures that are not part of any texture set [untyped], e.g., "Texture.png"


# Separating non-qualifying and qualifying sets:

# Collecting required maps for qualifying sets:
    available_required_maps_per_set: Dict[str, Set[str]] = {} # Stores available texture types required for packing modes per texture set.
    skipped_sets: Set[str] = set()

    for texture_id, entry in texture_sets.items():
        available_tex_types: Set[str] = set(entry["types"].keys()) # Gets all available texture types for a texture set.

        if not available_tex_types:
            skipped_sets.add(texture_id)
            continue
        # When a script wasn't able to derive known texture types for a texture set, or no texture set at all.

        available_required_maps: Set[str] = set() # Required texture types from qualifying modes that are actually present in this set.
        qualifies: bool = False

        for mode in valid_packing_modes:
            channels = (mode.get("channels", {}) or {})
            required_tex_types: Set[str] = set()
            for v in channels.values():
                if not v:
                    continue
                required_tex_types.add(_strip_channel_specifier(v).lower())  # Removes e.g., "_R" channel specifier for RGB maps, to see the general map type needed.
            if len(available_tex_types & required_tex_types) >= 2:
                qualifies = True
                available_required_maps.update(available_tex_types & required_tex_types)
        # Let's pass the set if it has at least two of this mode’s required maps.

        if qualifies: #
            available_required_maps_per_set[texture_id] = available_required_maps
        else:
            skipped_sets.add(texture_id)


 # Building skipped set for logging summary:
    for texture_id in skipped_sets:
        entry = texture_sets.get(texture_id)
        if not entry:
            continue
        group_folder, _ = texture_id.split(":", 1)
        display_set_name = entry["display_name"]

        display_files: Set[str] = set() # Set of all textures to be displayed "skipped" in logs.
        for file_list in entry["types"].values():
            for p in file_list:
                file_name = p.rsplit("/", 1)[-1]
                display_files.add(file_name)
        # For texture sets that didn't have required maps for any packing mode.

        for p in entry["untyped"]:
            file_name = p.rsplit("/", 1)[-1]
            display_files.add(file_name)
        # For textures not recognized as texture sets.

        pre_skipped_sets[group_folder][display_set_name] = sorted(display_files, key=str.lower)


# Updating context selection paths to store only textures actually required for channel_packaging modes:
    filtered: Dict[str, str] = {}
    for texture_id, needed_types in available_required_maps_per_set.items():
        entry = texture_sets[texture_id]
        for t in needed_types:
            for e in entry["types"].get(t, []):
                filtered[e] = ""  # Placeholder values; dict values are absolute paths to each file - created and overridden in "prepare_workspace".
    ctx.selection_paths = filtered


    return {grp: dict(names_map) for grp, names_map in pre_skipped_sets.items()}
    # Returns skipped sets for logging.


def _build_texture_sets(input_folder: str, initial_files: List[str], *, required_types_by_set: Optional[Dict[str, Set[str]]] = None, ctx: Optional[CPContext] = None) -> Dict[str, TextureSet]:
# Iterates over all given files, extracts texture set name, and collects all its map data.

    raw_textures: Dict[str, TextureSet] = {}
    for file in initial_files:
        full_path: str = os.path.join(input_folder, file)
        info = _extract_tex_set_name(full_path)
        if not info:
            continue

        tex_set_name, tex_type, declared_suffix, original_filename = info

        key: str = tex_set_name.lower()

        if required_types_by_set is not None:
            allowed = required_types_by_set.get(key, set())
            if allowed and tex_type.lower() not in allowed:
                continue
        # Filters only the files that are required for a given packing mode.

        resolution = _extract_tex_map_data(full_path)

        if key not in raw_textures:
            raw_textures[key] = TextureSet(tex_set_name=tex_set_name)
            # Assigns the original texture set name extracted by a function, only for the first map, as the key doesn't exist yet.

        texture_data = TextureMapData(
            path=full_path,
            resolution=resolution,
            suffix=declared_suffix,
            filename=original_filename,
        )

        raw_textures[key].available_maps[tex_type] = texture_data
        # Creates a TextureMapData instance extracted by function and add it to the appropriate map type list.

    raw_textures = dict(sorted(raw_textures.items(), key=lambda kv: kv[0]))
    return raw_textures




#                                          === Mode Selection & Resolution ===

def _strip_channel_specifier(name: str) -> str:
    # Removes the channel specifier (e.g., _R, .R) from the texture name and returns the base name in lowercase.
    return re.sub(r'[._]([rgba])$', '', name, flags=re.IGNORECASE).lower()


def _extract_mode_name(packing_mode: PackingMode) -> str:
    # Extracts the packing-mode suffix from the initial letters of each texture type mapped in channels.
    # Or uses the custom suffix if present in the config.

    custom_suffix: str = packing_mode.get("custom_suffix", "").strip()
    channels: ChannelMapping = packing_mode["channels"]

    if custom_suffix:
        return custom_suffix


    seen_maps: Set[str] = set() # Stores only unique maps (e.g., "Normal_R", "Normal_G", "Height" > "Normal", "Height").
    result: List[str] = []
    for channel in ["R", "G", "B", "A"]:
        tex: Optional[str] = channels.get(channel)
        if not tex:
            continue

        base_map: str = _strip_channel_specifier(tex) # Removes suffixes (e.g., "_R") from the texture type name if necessary.
        first_letter: str = base_map[0].upper()

        if base_map not in seen_maps:
            result.append(first_letter)
            seen_maps.add(base_map)
        # Adds only the first letters of unique texture types.
    return "".join(result)


def _get_available_maps_for_packing(mode: PackingMode, available_maps: TextureMapCollection) -> TextureMapCollection:
    # Returns a dict of available map types used in each PackingMode.
    # If a set contains the same map type in multiple resolutions, picks the highest for packing.
    required_maps = mode.get("channels", {}) # Retrieves the mapping of texture types to RGBA channels from PACKING_MODES; a cast is required due to TypedDict.
    result: TextureMapCollection = {}

    for tex_type in set(required_maps.values()):
        if not tex_type:
            continue

        base_type = _strip_channel_specifier(tex_type)  # Gets the base map name (removing the channel suffix like "_R").

        if base_type in available_maps:
            maps_list = available_maps[base_type]
            if isinstance(maps_list, list):
                best = max(maps_list, key=lambda t: t.resolution[0] * t.resolution[1])
            else:
                best = maps_list
            result[base_type] = best
        # Picks the largest resolution for the same map type.
    return result

    # e.g., in ARM packaging from:   Albedo : [D], Roughness : [D], Metallic : [D], AO : [D]
    #                               returns :      Roughness : [D], Metallic : [D], AO : [D]


def _get_valid_modes_for_set(original_name: str, available_maps: TextureMapCollection, valid_packing_modes: List[PackingMode]) -> List[ValidModeEntry]:
# Adds only the texture maps used to a packing mode that expects them and also adds its case-sensitive texture set name for easier identification of a processed set during generation of the channel-packed image.
# Requires at least 2 maps for a packing mode.
    valid_modes_with_maps: List[ValidModeEntry] = []
    for mode in valid_packing_modes:
        maps_for_mode: TextureMapCollection = _get_available_maps_for_packing(mode, available_maps)

        if len(maps_for_mode) < 2:
            continue
        valid_modes_with_maps.append(
            ValidModeEntry(
                tex_set_name=original_name,
                mode=mode,
                maps_for_mode=maps_for_mode,
                suffix=_extract_mode_name(mode)
            )
        )
    return valid_modes_with_maps


def _check_textures_and_pick_target_resolution(
    maps_for_mode: TextureMapCollection,
    strategy: str,
    mode_name: Optional[str] = None,
) -> Tuple[bool, Tuple[int, int]]:
# Checks resolution of all textures required by a packing mode, picks one according to the resize strategy set in .JSON case of res mismatch.
# Also skips packing mode for a set if it encounters texture resolution that is not 2^n mip-map friendly, and returns errors to be logged in summarize_mode_results

    name = mode_name or ""

    resolutions: List[Tuple[int, int]] = []
    total = len(maps_for_mode)
    for tex in maps_for_mode.values():
        if tex.resolution is not None:
            resolutions.append(tex.resolution)
    # Lists all resolutions.


    if not resolutions or len(resolutions) < total:
        return False, (0, 0)
    # Skips mode if files are corrupted.


    min_res = min(resolutions, key=lambda r: r[0] * r[1])
    max_res = max(resolutions, key=lambda r: r[0] * r[1])


    if not is_power_of_two(min_res[0]) or not is_power_of_two(min_res[1]):
        return False, min_res
    # Skips a texture set if any texture doesn't have 2^n resolution.


    if len(set(resolutions)) == 1:
        return True, min_res
    # When all textures have the same resolution.


    if strategy == "up":
        return True, max_res
    else:
        return True, min_res
    # In case of resolution mismatch.


def _list_textures_to_scale(
    maps_for_mode: TextureMapCollection,
    target_size: Tuple[int, int]
) -> List[MapNameAndRes]:
# Lists all filenames and their resolutions for texture maps that are mismatched and need to be scaled.

    textures_to_scale: List[MapNameAndRes] = []
    for tex in maps_for_mode.values():
        if tex.resolution != target_size:
            textures_to_scale.append(MapNameAndRes(tex.filename, tex.resolution))
    return textures_to_scale




#                                        === Warnings / Logging ===

def _check_suffix_warnings_for_set(maps_for_mode: TextureMapCollection) -> List[MapNameAndRes]:
# Iterates over all textures required by a packing mode to check whether there is a mismatch between the declared size in the name (if given) and the actual file resolution.

    warnings: List[MapNameAndRes] = []
    for tex in maps_for_mode.values():
        result = check_texture_suffix_mismatch(tex)
        if result:
            warnings.append(result)
    return warnings


def _list_missing_maps_for_channel_mapping(channel_mapping: ChannelMapping, maps_for_mode: TextureMapCollection) -> List[str]:
# Lists all textures that are missing for a packing mode to be displayed in logs.
    available_lower: Set[str] = {k.lower() for k in maps_for_mode.keys()}
    missing_maps: List[str] = []
    for value in channel_mapping.values():
        if not value:
            continue
        base_map: str = value.split(".")[0].lower()
        if base_map not in available_lower:
            missing_maps.append(base_map)
    return missing_maps


def _print_warnings(
    items: Union[List[MapNameAndRes], List[str]],
    displayed_flag: bool,
    warning_type: str, # "resolution" | "suffix" | "missing_maps"
    *,
    target_resolution: Optional[Tuple[int, int]] = None, # only for "resolution"
    mode_name: Optional[str] = None, # Only for "missing_maps"
) -> bool:

    if not items:
        return displayed_flag

    if warning_type == "resolution":
        simplified = "Warning: Texture set resolution mismatch."
        detailed   = f"Warning: Texture set resolution mismatch:\n   Resize strategy set to '{RESIZE_STRATEGY}'"
    elif warning_type == "suffix":
        simplified = "Warning: Suffix resolution mismatch."
        detailed   = "Warning: Suffix resolution mismatch:"
    elif warning_type == "missing_maps":
        mn = mode_name or ""
        simplified = f"Warning: Missing some texture maps for '{mn}'."
        detailed   = f"Warning: Missing texture maps for '{mn}':"
    else:
        simplified = detailed = "Warning."
    # Chooses a warning type to print.


    if not displayed_flag:
        log(detailed if SHOW_DETAILS else simplified, "warn")
        # Prints warning.
        displayed_flag = True
    # Displays the main warning type only once per texture set.

    if SHOW_DETAILS:
        if warning_type == "resolution":
            if target_resolution:
                tw, th = target_resolution
                for item in items:
                    w, h = item.resolution
                    log(f"Rescaling {item.filename} ({w}x{h}) to {tw}x{th}", "info")
                    # Prints info.
        elif warning_type == "suffix":
            for item in items:
                w, h = item.resolution
                log(f"{item.filename} but it's {w}x{h}", "info")
                # Prints info.
        elif warning_type == "missing_maps":
            for miss in items:
                for original_key in TEXTURE_CONFIG.keys():
                    if original_key.lower() == miss:
                        log(f"Generated: {original_key}", "info")
                        # Prints info.
                        break

    # Prints warnings for each affected file if SHOW_DETAILS was set to true.
    return displayed_flag




#                                              === Generation ===

def _extract_channel(image: Optional[ImageObj], tex_map_type_name: str) -> Optional[ImageObj]:
# Extracts the channel specified by the packing mode from an RGB/RGBA image. e.g., B: Normal_R. > Normal red channel
    if image is None:
        return None

    match = re.search(r'[._]([rgba])$', tex_map_type_name, re.IGNORECASE)
    requested_channel: str = match.group(1).upper() if match else ""
    # Derives the texture type name from PACKING_MODE channel values (e.g., Normal_R).

    if get_mode(image) == "L":
        return image
    # If the image is grayscale, returns it as-is.


    if requested_channel and requested_channel in get_bands(image):
        return get_channel(image, requested_channel)
    # If the image is RGB/RGBA and the requested channel is valid, extracts that channel.


    return to_grayscale(image)
    # Otherwise, converts the image to grayscale as a fallback.


def _generate_channel_packed_texture(
    valid_mode_entry: ValidModeEntry, # Original name - mode (name, custom_suffix, channels) - maps used for this mode (tex type: [(path, resolution=, suffix, filename, ext)]).
    target_resolution: Dict[str, Tuple[int, int]], # Final resolution for each mode that files are going to be generated to, according to RESIZE_STRATEGY from config.
    out_dir: str, # Absolute path to a folder where textures are generated.
    bak_dir: Optional[str] = None, # Absolute path to a folder where textures used for generation are moved afterward.
    ctx: Optional[CPContext] = None
) -> Optional[str]: # Returns the file name of created map and texture types that needed to be generated in case of missing.

    mode: PackingMode = valid_mode_entry.mode # Packing mode that is valid due to check for necessary maps earlier.
    maps_for_mode: TextureMapCollection = valid_mode_entry.maps_for_mode # Only maps that are required by the current packing mode and their corresponding data (tex type: [(path, resolution=, suffix, filename, ext)]).
    mode_name: str = mode["mode_name"].strip() # Name of packing mode e.g., ARM.
    base_size: tuple[int, int] = target_resolution.get(mode_name, (0, 0)) # Setting target resolution for all files during generation.
    missing_maps: List[str] = [] # Lists all texture's set missing maps required for a given packing mode.
    loaded_images: Dict[str, Optional[ImageObj]] = {} # Stores loaded texture maps by type, e.g., {"Albedo": <PIL.Image.Image image mode=L size=2048x2048>, "Normal": <PIL.Image.Image image mode=RGB size=2048x2048>, "Roughness": None}.
    channels: List[ImageObj] = [] # List of all images collected to generate the final image.
    packed: Optional[ImageObj] = None # Final generated image.



# Preparing an output file type:
    channels_cfg = cast(Dict[str, str], mode["channels"]) # Variable cast due to TypedDict > Dict issue
    alpha = channels_cfg.get("A") # Tries to fetch texture mapped to the Alpha channel.
    order = "RGBA" if alpha else "RGB" # Decides whether the output image should have 3 or 4 channels,
    channel_order: List[Tuple[str, str]] = [(c, channels_cfg[c]) for c in order] # Builds a list of (channel, texture_name) mappings for the output image.
    generated_file_type: str = order  # "RGB" or "RGBA
    # Output file type


# Loading texture maps:
    try:
        for tex_name, tex_data in maps_for_mode.items():
            try:
                im = open_image(tex_data.path)
                if get_size(im) != base_size:
                    im_resized = resize(im, base_size)
                    close_image(im) #  # Explicitly closes the original image after resizing; the wrapper has no context manager, otherwise the file handle stays open.
                    im = im_resized
                loaded_images[tex_name] = im # Maps image data to corresponding a texture name.
            except (OSError, ValueError) as e:
                log(f"Warning: failed to open '{tex_data.path}' ({e}), will use default.", "warn")
                # Prints warning.
                loaded_images[tex_name] = None


# Scaling all textures to the target size (if mismatched resolutions) and filling in default values if missing any texture maps:
        for tex_name in maps_for_mode.keys():
            img: Optional[ImageObj] = loaded_images.get(tex_name)
            if img is None:
                map_type: str
                default_val: int
                map_type, default_val = TEXTURE_CONFIG[tex_name.lower()]["default"] # From the tuple (G/RGB, int), takes only the default fill value.
                loaded_images[tex_name] = new_gray(base_size, default_val)
                missing_maps.append(tex_name)
            # Gets default values for each map type from config and creates a missing map for packing if necessary.
            elif get_size(img) != base_size:
                loaded_images[tex_name] = resize(img, base_size)
            # Scales all textures to the target size, if mismatched resolutions.


# Collecting images for each final image channel:
        for _, map_name in channel_order:
            tex_key: str = map_name.split(".")[0].lower()
            im: ImageObj = loaded_images.get(tex_key)

            if im is None:
                default_val: int = 128  # Default fallback, needed for type validation.
                for key, cfg in TEXTURE_CONFIG.items():
                    if key.lower() == tex_key:
                        default_val: int = cfg["default"][1] # Uses default fill value of a corresponding map, e.g., ("RGB", 128)
                        break
                im = new_gray(base_size, default_val)
                missing_maps.append(tex_key)
            # Creates maps with derived default values if missing; case-insensitive.

            channel_img: ImageObj = _extract_channel(im, map_name) # Passes a chosen texture map if grayscale, if RGB, then extracts specific channel, derived from .R .G .B in its name.
            channels.append(channel_img)

# Generating the final image:
        packed = merge_channels(generated_file_type, channels)


# Saving the file:
        display_name: str = valid_mode_entry.tex_set_name # Case-sensitive texture set name, e.g., "Wall".
        packing_mode_suffix: str = valid_mode_entry.suffix.strip()
        resolution_suffix: str = (f"_{resolution_to_suffix(base_size)}" if any(tex.suffix for tex in maps_for_mode.values()) else "") # Only if the original file name also has size suffix.
        filename: str = f"{display_name}_{packing_mode_suffix}{resolution_suffix}"

        save_image(packed, out_dir, filename, mode_name, ctx)


        if bak_dir:
            for tex_data in maps_for_mode.values():
                move_used_map(tex_data.path, bak_dir, ctx)
        return filename


    finally:
        items = list(loaded_images.values()) + channels
        if packed is not None:
            items.append(packed)
        close_image_files(items)
    # Safely closes all opened images even if there is an error during image processing. The wrapper has no context manager, otherwise the file handle stays open.




#                                        === Reporting / Summary ===

def _summarize_mode_results(
    original_tex_set_name: str,
    valid_packing_modes: List[PackingMode],
    valid_modes_with_maps: List[ValidModeEntry],
    target_resolution: Dict[str, Tuple[int, int]],
    *,
    invalid_modes: Optional[Set[str]] = None, # Modes that textures that had invalid textures.
    invalid_mode_dims: Optional[Dict[str, Tuple[int, int]]] = None,
ctx: Optional[CPContext] = None,
    log_prefix: str = ""
) -> None:
# Summarizes all Packing Modes for a given texture set – lists skipped and completed modes.

    invalid = invalid_modes or set()
    dims = invalid_mode_dims or {}


    for mode in valid_packing_modes:
        mode_name: str = mode["mode_name"]

        if mode_name in invalid:
            w, h = dims.get(mode_name, (0, 0))
            if (w, h) == (0, 0):
                log(f"{log_prefix} Error: '{mode_name}' Corrupted files missing resolution info - skipping mode.", "error")
                # Prints error.

            else:
                if SHOW_DETAILS:
                    log(f"{log_prefix} Error: '{mode_name}' Invalid resolution ({w}x{h}) - skipping mode.", "error")
                else:
                    log(f"{log_prefix} Error: '{mode_name}' Invalid resolution - skipping mode.", "error")
                # Prints error.
            continue

        entry_match = next((e for e in valid_modes_with_maps if e.mode["mode_name"] == mode_name), None)
        # Skips invalid modes, so they are not logged again in the summary,

        if entry_match:
            ext: str = ctx.export_ext
            filename = f"{original_tex_set_name}_{entry_match.suffix}.{ext}"
            status_res = target_resolution.get(mode_name, (0, 0))
            if status_res != (0, 0):
                if SHOW_DETAILS:
                    w2, h2 = status_res
                    log(f"{log_prefix} Created: {filename} ({w2}x{h2})", "complete")
                    #  Prints completed.
                else:
                    log(f"{log_prefix} Created: {filename}", "complete")
                    #  Prints completed.
        else:
            if SHOW_DETAILS:
                log(f"{log_prefix} Skipped: {original_tex_set_name}_{mode_name} (less than 2 maps for a mode)", "skip")
            else:
                log(f"{log_prefix} Skipped: {original_tex_set_name}_{mode_name}", "skip")
                # Prints skipped.




#                                         === CLI entry point ===

def main() -> None:
    cli_arg = " ".join(sys.argv[1:]).strip() or None
    # Allows a CLI path to override INPUT_FOLDER.
    input_folder = (cli_arg or INPUT_FOLDER or "").strip()
    if not input_folder or not os.path.isdir(input_folder):
        log("Aborted: No valid input folder provided (CLI/config).", "error")
        # Prints error.
        sys.exit(1)


    channel_packer(input_folder)

if __name__ == "__main__":
    main()