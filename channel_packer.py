
""" Generates channel-packed textures from source maps according to the configuration. """

import os
import sys
import re
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple, Union, cast


from backend.image_lib import (ImageObject, close_image, get_image_channels, get_channel,
                               get_image_mode, get_size, is_grayscale, is_rgb_grayscale, merge_channels, new_image_grayscale, open_image, resize, convert_to_grayscale)

from backend.texture_classes import (ChannelMapping, MapNameAndResolution, PackingMode, SetEntry,
                                     TextureMapCollection, TextureMapData, TextureSetInfo, TextureSet, ValidModeEntry)

from backend.io_backend import (ConvertedEXRImage, CPContext, context_validate_export_extension, split_by_parent,
                                list_initial_files, prepare_workspace, save_generated_texture, move_used_map, cleanup)

from settings import (TextureTypeConfig, ALLOWED_FILE_TYPES, BACKUP_FOLDER_NAME, TARGET_FOLDER_NAME, INPUT_FOLDER, PACKING_MODES, RESIZE_STRATEGY, SHOW_DETAILS, TEXTURE_CONFIG)

from utils import (check_texture_suffix_mismatch, close_image_files, detect_size_suffix,
     group_paths_by_folder, is_power_of_two, log, make_output_dirs, match_suffixes, resolution_to_suffix, validate_safe_folder_name)




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
# Using an external context keeps the main function backend-agnostic.

    context = CPContext() # Context object holding the runtime state for the currently processed files.
    start_time = time.time()
    packed_any_textures: bool = False


# If provided, taking into account the input folder specified via CLI:
    if input_folder:
        folder: str = os.path.abspath(input_folder)
    if hasattr(context, "input_folder"):
        setattr(context, "input_folder", folder)


# Validating config and setting up the files:
    _validate_config(RESIZE_STRATEGY, context) # Validate base settings (export ext, resize strategy).
    valid_packing_modes: List[PackingMode] = _validate_packing_modes()  # Validates packing modes from the config, fixes channel mappings, and skips empty/invalid entries:


    pre_skipped_texture_sets_summary: Dict[str, Dict[str, List[str]]] = _validate_and_setup_files(input_folder or "", context, valid_packing_modes) # Validates and collects the files via backend into the context for further processing.
    # Returns skipped sets that don't have enough required maps for logging purposes at the end.
    work_directory: str = context.work_directory  # Absolute working directory for processing the files.




# Grouping each subfolder to a separate package:
    grouped_files: Dict[str, List[str]] = split_by_parent(context)  # Creates a dict that groups files by their parent directory relative to ROOT ("."), e.g., {'.': [...], {'A': [...], 'A/B': [...],}
    multiple_file_groups: int = len(grouped_files) > 1
    processed_file_groups_order: List[str] = [] # Record the processing order of groups so the final "Skipped" logs follow the same sequence.


    for relative_parent_path, files_in_group in grouped_files.items():
        final_folder_path: str = work_directory if relative_parent_path == "." else os.path.join(work_directory, relative_parent_path)
        final_folder_path = os.path.abspath(final_folder_path)
        target_directory, backup_directory = make_output_dirs(final_folder_path, target_folder_name = TARGET_FOLDER_NAME, backup_folder_name = BACKUP_FOLDER_NAME)
    # Creates output and backup folders (if set) per texture set's folder.


        if multiple_file_groups:
            log_prefix = "[ROOT] " if relative_parent_path == "." else f"[{relative_parent_path}] "
        else:
            log_prefix = ""
        # Adds folder prefix for logging if source textures are in more than one folder.


# Collecting maps into texture sets for each folder:
        raw_textures: Dict[str, TextureSet] = _build_texture_sets(final_folder_path, files_in_group, context = context) # Collecting maps into texture sets data.
        processed_file_groups_order.append(relative_parent_path)

# Filtering modes to those with at least two required maps, then choosing the target resolution and logging any mismatches.
        for texture_set_name, texture_set in raw_textures.items():
            original_texture_set_name: str = texture_set.texture_set_name

            texture_name_for_log = f"{log_prefix}{original_texture_set_name}" if log_prefix else original_texture_set_name
            log(f"\nProcessing: {texture_name_for_log}", "info")
            # Prints info.

            available_texture_maps: TextureMapCollection = texture_set.available_texture_maps

            valid_packing_modes_with_maps: List[ValidModeEntry] = _get_valid_modes_for_set(original_texture_set_name, available_texture_maps, valid_packing_modes)
            # Collects packing modes applicable to this set (requires at least two maps).

            expected_texture_resolution: Dict[str, Tuple[int, int]] = {}
            # Chosen target resolution for each mode; used to normalize map sizes and log mismatch.

            suffix_mismatch_buffer: List[MapNameAndResolution] = []
            displayed_global_suffix_warning: bool = False
            displayed_global_resolution_warning: bool = False
            # Log buffers (per set).

            if not valid_packing_modes_with_maps:
                texture_set.completed = True
                continue
            # Skips if there are less than 2 necessary maps for a given packing mode; the flag is used in logs.


            invalid_mode_names_for_set: Set[str] = set() # Collects modes that do not meet any criteria, used to create final valid_modes_with_maps.
            invalid_resolution_for_summary: Dict[str, Tuple[int, int]] = {}  # Invalid dimensions for textures that are not 2^n

            for packing_mode in valid_packing_modes_with_maps:
                texture_maps_for_mode: TextureMapCollection = packing_mode.texture_maps_for_mode

                is_valid, target_resolution = _check_textures_and_pick_target_resolution(
                    texture_maps_for_mode = texture_maps_for_mode,
                    resize_strategy = RESIZE_STRATEGY,
                    packing_mode_name = packing_mode.mode["mode_name"],
                )
                # Finds listed textures resolutions and checks if textures have mip friendly power-of-two (2^n) res.

                if not is_valid:
                    invalid_mode_names_for_set.add(packing_mode.mode["mode_name"])
                    invalid_resolution_for_summary[packing_mode.mode["mode_name"]] = target_resolution
                    continue
                # # Returns mode names whose textures either have an incorrect resolution or are corrupted (reported as 0×0).


                textures_to_scale: List[MapNameAndResolution] = _list_textures_to_scale(texture_maps_for_mode, target_resolution) # Lists all textures with mismatched resolutions that need to be scaled before channel packing.

                displayed_global_resolution_warning = _print_warnings(
                    textures_to_scale,
                    displayed_global_resolution_warning,
                    warning_type = "resolution",
                    target_resolution = target_resolution,
                )
                # Prints warning if textures have different resolution within a single texture set.

                suffix_mismatch_buffer.extend(_check_suffix_warnings_for_set(texture_maps_for_mode))
                # Lists all files whose size suffix (if available) does not match the actual resolution.

                expected_texture_resolution[packing_mode.mode["mode_name"]] = target_resolution
                # Sets a target resolution for a given packing mode.


 # Finalizing modes for a set and printing warnings before generation:
            ready_packing_modes = [
                mode for mode in valid_packing_modes_with_maps
                if mode.mode["mode_name"] not in invalid_mode_names_for_set
            ]
            invalid_packing_modes = [
                mode for mode in valid_packing_modes_with_maps
                if mode.mode["mode_name"] in invalid_mode_names_for_set
            ]

            if not ready_packing_modes:
                texture_set.completed = True
                # Case when all the modes have invalid textures resolution.
                # Marks them as completed, not to be displayed in logs as skipped due to not having required maps, even though no channel_packed textures were generated.
                if invalid_packing_modes:
                    texture_set.processed = True
                valid_packing_modes_with_maps = []
                # Skips mode if it doesn't have any valid modes, so the "Skipped: _mode" isn't generated for them,
                # Instead all texture sets that have no valid packing modes go into the general"skipped"" summary at the end of the logs.
            else:
                valid_packing_modes_with_maps = ready_packing_modes

            displayed_global_suffix_warning = _print_warnings(
                suffix_mismatch_buffer,
                displayed_global_suffix_warning,
                warning_type="suffix",
            )
            # Prints warning if size suffixes in the file name (if present) do not match the actual texture size


            texture_set_name_lower: str = original_texture_set_name.lower()
            converted_exr_textures_list: List[str] = sorted({selected.texture_type for selected in context.textures_converted_from_raw.values() if selected.texture_set_name == texture_set_name_lower and selected.texture_type is not None})

            _print_warnings(
                warning_items=converted_exr_textures_list,
                warning_displayed=False,
                warning_type="exr_source",
            )
            # Logs files that were converted from float to 8bit int.


            for packing_mode in valid_packing_modes_with_maps:
                channel_mapping: ChannelMapping = packing_mode.mode.get("channels", {})
                missing_textures = _list_missing_texture_maps_for_channel_mapping(channel_mapping, packing_mode.texture_maps_for_mode)

                _print_warnings(
                    missing_textures,
                    False,  # Prints only once per mode
                    warning_type="missing_maps",
                    packing_mode_name=packing_mode.mode.get("mode_name", ""),
                )
             # Prints warning if size suffixes in the file name (if present) do not match the actual texture size.


# Generating channel packed texture:
            for packing_mode in valid_packing_modes_with_maps:
                filename = _generate_channel_packed_texture(
                    packing_mode,
                    expected_texture_resolution,
                    target_directory,
                    backup_directory,
                    context
                )
                if filename:
                    texture_set.processed = True
                    texture_set.completed = True
                    packed_any_textures = True


            _summarize_mode_results(
                original_texture_set_name,
                valid_packing_modes,
                valid_packing_modes_with_maps,
                expected_texture_resolution,
                context=context,
                invalid_packing_modes=invalid_mode_names_for_set,
                invalid_packing_mode_dimensions=invalid_resolution_for_summary,
                log_prefix=log_prefix
            )


# Printing summary logs for all the processed folders, and cleaning up temporary files:
    if pre_skipped_texture_sets_summary:
        log("", "info")  # Visual separator
        final_group_summary_order: List[str] = [g for g in processed_file_groups_order if g in pre_skipped_texture_sets_summary] + [g for g in pre_skipped_texture_sets_summary.keys() if g not in processed_file_groups_order]
        # Lists texture sets and their available maps skipped due to missing required maps + textures sets and their maps from folders that produced no successfully packed textures.

        for relative_parent_path in final_group_summary_order:
            skipped_files_by_set = pre_skipped_texture_sets_summary[relative_parent_path]
            folder_prefix = f"[{'ROOT' if relative_parent_path in ('.', '') else relative_parent_path}] " if multiple_file_groups else "" # Adds folder prefix only when run with multiple folders. Also displays "ROOT" for the root folder.
            for display_name, files in skipped_files_by_set.items():
                details = f" (files: {', '.join(files)})" if SHOW_DETAILS and files else ""
                log(f"Info: {folder_prefix}Skipped '{display_name}' set – missing required maps{details}", "info")
    # Prints skipped only for texture sets that had not enough maps for any of the set channel packing modes.


    log("", "info")  # Visual separator
    log("All processing done.", "complete")


    if TARGET_FOLDER_NAME.strip() and packed_any_textures:
        if multiple_file_groups:
            log(f"Packed maps saved to '{TARGET_FOLDER_NAME}' subfolder(s) inside processed folders.", "info")
        else:
            single_group = next(iter(grouped_files))
            final_folder_path = work_directory if single_group in (".", "") else os.path.join(work_directory, single_group) # In case the only processed textures were in the subfolder.
            absolute_target_directory = os.path.abspath(os.path.join(final_folder_path, TARGET_FOLDER_NAME))
            log(f"Packed maps saved to: {absolute_target_directory}", "info")
        # For a single group run prints the absolute output path for the only processed folder.

    if BACKUP_FOLDER_NAME.strip() and packed_any_textures:
        if multiple_file_groups:
            log(f"Source maps moved to backup folder '{BACKUP_FOLDER_NAME}' inside processed folders.", "info")
        else:
            single_group = next(iter(grouped_files))
            final_folder_path = work_directory if single_group in (".", "") else os.path.join(work_directory, single_group) # In case the only processed textures were in the subfolder.
            absolute_backup_directory = os.path.abspath(os.path.join(final_folder_path, BACKUP_FOLDER_NAME))
            log(f"Source maps moved to: {absolute_backup_directory}", "info")
        # For a single group run prints the absolute output path for the only processed folder.
    # Displays summary logs for single as well as multiple folders.


    cleanup(context)
    # Deletes UE temporary files or used files on Windows.


    if SHOW_DETAILS:
        elapsed_time = time.time() - start_time
        log(f"Execution time: {elapsed_time:.2f} seconds", "info")
        # Prints info.




#                                       === Validation & Setup ===

def _validate_config(resize_strategy: str, context: Optional[CPContext] = None) -> None:
# Runs initial validation for the config.
# Later on the config is checked for packing mode validity when running valid_modes for each packing mode.

    validate_safe_folder_name(BACKUP_FOLDER_NAME)
    validate_safe_folder_name(TARGET_FOLDER_NAME)
    # Checks if folder names don't contain unsupported characters.


    context_validate_export_extension(context)
    output_file_extension: str = context.export_extension.lower()
    if not output_file_extension:
        log("Aborted: could not resolve output extension.", "error")
        raise SystemExit(1)
    # Validates and stores chosen export extension.

    uses_alpha = any((mode.get("channels") or {}).get("A") for mode in PACKING_MODES)

    if uses_alpha and output_file_extension in ("jpg", "jpeg", "jfif"):
        log(
            f"Aborted: Texture is mapped to the Alpha channel, but selected file type '{output_file_extension}' does not support alpha. Change FILE_TYPE to 'png' or 'tga' and retry.","error",)
        raise SystemExit(1)
    # Raises an error when texture is mapped to alpha, but output file type doesn't support it.


    resize_strategy: str = (resize_strategy or "").lower()
    if resize_strategy not in ("up", "down"):
        log(f"Warning: Unknown RESIZE_STRATEGY '{resize_strategy}'. Defaulting to 'down'.", "warn")
        # Prints Warning.
    # Checks for valid resize strategy in settings.
    return


def _validate_and_setup_files(input_folder: str, context: CPContext, valid_packing_modes: Optional[List["PackingMode"]] = None, ) -> Dict[str, Dict[str, List[str]]]:
# Prepares the files for further processing. Takes only the files that are actually used for the generation of the channel-packed maps.
# For Unreal Engine it exports files from the Content Browser to the temporary folder.
# For summary in logs, it returns texture sets that didn't have any texture maps needed for any of the packing modes.


    effective_input: str = input_folder or "" # Allows overriding an input path set in config when run from the system's CLI.
    initial_files: List[str] = list_initial_files(context)

    if not initial_files:
        valid_output_file_extensions: str = ", ".join((e if str(e).startswith(".") else f".{e}") for e in ALLOWED_FILE_TYPES)
        log(f"Aborted: No input files matching {valid_output_file_extensions} in: {input_folder}", "error")
        raise SystemExit(1)


    pre_skipped_texture_sets_summary: Dict[str, Dict[str, List[str]]] = {} # Texture sets that didn't have any texture maps needed for any of the packing modes.

    if valid_packing_modes:
        pre_skipped_texture_sets_summary = _preselect_required_textures(valid_packing_modes, context)
    # Limits the file selection to textures required by the packing modes.


    prepare_workspace(context)
    # When run from the Engine, exports textures to a temporary workspace.
    # Resolves and fills absolute disk paths for each relative/package-path key.

    work_directory = context.work_directory
    if not (work_directory and os.path.isdir(work_directory)):
        log(f"Aborted: Working directory does not exist: {work_directory}", "error")
        raise SystemExit(1)
    return pre_skipped_texture_sets_summary


def _validate_packing_modes() -> List[PackingMode]:
    # Checks whether channels are mapped properly in the config; if not, tries to fix them or aborts execution.
    # If an RGB texture is mapped to a channel without an explicit component, defaults to the destination channel (e.g., R: Normal > R: Normal_R).
    # Removes unnecessary channel suffixes from grayscale maps (e.g., Height_R).
    # Returns PACKING_MODES with fixes applied.


    valid_packing_modes: list[PackingMode] = []
    for mode in PACKING_MODES:
        packing_mode_name: str = mode.get("mode_name", "").strip()
        if not packing_mode_name:
            continue
        # Considers a packing mode valid only if it has a mode name.

        channels: ChannelMapping = mode.get("channels", {})
        normalized_channels: dict[str, str] = {}  # In case a grayscale texture map has an unnecessary suffix (e.g., R).

        for channel in ("R", "G", "B", "A"):
            channel_value: Optional[str] = channels.get(channel)

            if not channel_value:
                if channel == "A":
                    continue
                    # Alpha may be empty.
                else:
                    log(f"PACKING_MODE '{packing_mode_name}' is missing required channel '{channel}'", "error")
                    # Prints error.
                    sys.exit(1)
            # Allows missing channel mapping only for Alpha; otherwise the script stops.

            match: Optional[re.Match[str]] = re.match(r"([a-z0-9]+)([._]?[rgb]?)$", channel_value.strip(), re.IGNORECASE)
            # Extracts the map name and optional channel suffix using a regex.

            if not match:
                log(f"PACKING_MODE '{packing_mode_name}' invalid syntax in channel '{channel}': {channel_value}", "error")
                # Prints error.
                sys.exit(1)

            texture_name: str = match.group(1).lower() # Derives map name.
            channel_component_specifier: str = match.group(2).lower() # Derives suffix or "".

            texture_config: Optional[TextureTypeConfig] = next(
                (config for texture_type_name, config in TEXTURE_CONFIG.items() if texture_type_name.lower() == texture_name),
                None
            )
            # Returns the first texture type (key and config) that matches the derived map name in TEXTURE_CONFIG.

            if texture_config is None:
                log(f"PACKING_MODE '{packing_mode_name}' has unknown texture type set in {channel}: {channel_value}", "error")
                # Prints error.
                sys.exit(1)

            texture_type, _default_value = texture_config["default"]  # Fetches the map type (Grayscale or RGB).
            normalized_value: str = channel_value or "" # Texture mapped to a channel with a proper component specified in the case of RGB textures and without any for the grayscale.

            if texture_type.upper() == "RGB":
                if channel_component_specifier == "":
                    if channel in ("R", "G", "B"):
                        # If an RGB map lacks an explicit component, defaults to the destination channel (.r/.g/.b).
                        normalized_value: str = f"{texture_name}.{channel.lower()}"
                        if SHOW_DETAILS:
                            log(
                                f"PACKING_MODE '{packing_mode_name}' channel '{channel}': "
                                f"'{channel_value}' is RGB without channel, defaulting to '{normalized_value}'.","info")
                            # Prints info.
                    elif channel == "A":
                        log(
                            f"PACKING_MODE '{packing_mode_name}' channel '{channel}' is assigned full RGB map '{channel_value}' "
                            f"without explicit channel; Alpha must reference a single channel (e.g. '{channel_value}.r') "
                            f"or a grayscale map, or be empty.","error")
                        sys.exit(1)
                        # Prints info.
                        # Cannot infer which component should map to Alpha; aborts.
                else:
                    normalized_value = channel_value
                # Maps an RGB texture with a proper channel suffix.
            elif texture_type.upper() == "G":
                normalized_value = texture_name if channel_component_specifier != "" else channel_value
            # Removes unnecessary suffixes from grayscale maps.

            else:
                normalized_value = channel_value
            normalized_channels[channel] = normalized_value

        # Replaces the channels dict with the fixed mapping (e.g., when a grayscale map has a channel suffix).
        mode["channels"] = cast(ChannelMapping, normalized_channels)
        valid_packing_modes.append(mode)
    return valid_packing_modes




#                                         === Data Building ===

def _extract_info_from_texture_set_name(file_path_or_asset: str) -> Optional[TextureSetInfo]:
# Extracts info from the texture's name without opening the file - works with both files on a disc and Unreal's Content Browser paths.

    file_path: str = os.path.basename(file_path_or_asset)
    file_name, _ = os.path.splitext(file_path)  # Gets the filename without extension
    file_name_lower: str = file_name.lower()
    size_suffix: Optional[str] = detect_size_suffix(file_name)


    for texture_type, config in TEXTURE_CONFIG.items(): # Derives texture type and size suffixes based on their aliases set in settings.
        texture_type = texture_type.lower()
        suffixes_lower = [s.lower() for s in config["suffixes"]]
        for type_suffix in suffixes_lower:
            pattern = match_suffixes(file_name_lower, type_suffix, size_suffix or None)
            if not pattern:
                continue
            regex = re.compile(pattern, flags = re.IGNORECASE)
            match = regex.search(file_name)
            if not match:
                continue
            # Tries to match extracted regex (type/size suffix permutation) to match with a file name.

            texture_set_name = file_name[:match.start()].rstrip("_-.") # Texture set name before the found suffix
            return (texture_set_name, texture_type, (size_suffix or "").lower(), file_name)
    return None


def _extract_image_data(file_path: str) -> Optional[Tuple[int, int]]:
# Opens image to derive its actual resolution.
    try:
        image = open_image(file_path)
        resolution = get_size(image)
        close_image(image)
        return resolution
    except (OSError, ValueError) as e:
        log(f"Cannot open image file: {file_path} – {e}", "error")
        return None


def _preselect_required_textures(valid_packing_modes: List["PackingMode"], context: "CPContext", ) -> Dict[str, Dict[str, List[str]]]:
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


    packed_textures_suffixes = {_extract_mode_name(mode).upper() for mode in valid_packing_modes if _extract_mode_name(mode)} # Get final suffixes for the created channel packed, to filter out maps that could already be there from previous script run.

# Collecting all the texture sets and their files into unique grouped sets:
    texture_sets: Dict[str, SetEntry] = {} # Collection of texture sets where key is the unique id derived from textures paths and set names. Contains display_name, recognized map types : lists of file paths, and untyped files.
    pre_skipped_texture_sets: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list)) # Mapping of textures sets and its texture set, grouped by unique id, listing texture sets skipped due to not having rewired maps for any valid packaging mode.
    grouped = group_paths_by_folder(context.selection_paths_map.keys())  # Groups file paths by their parent folders relative to the root fodler.

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
            if maybe_suffix in packed_textures_suffixes:
                continue
            # Filters out files that have suffix matching of the currently channel packed textures, to omin files that could be from the previous script runs.


            extracted_info: Optional[Tuple[str, str, str, str]] = _extract_info_from_texture_set_name(asset_name)
            if extracted_info:
                texture_set_name, texture_type, _, _ = extracted_info
                texture_id = f"{group_folder}:{texture_set_name.lower()}"
                # Unique id built from the parent folder and set name; avoids collisions when identical set names exist in different directories.

                entry = texture_sets.get(texture_id)
                if entry is None: # Creates a texture set entry for every first texture from the same texture set.
                    entry = SetEntry(display_name = texture_set_name, types = {}, untyped = [])
                    texture_sets[texture_id] = entry
                entry["types"].setdefault(texture_type.lower(), []).append(key) # Assigns a texture type, and its file path to a texture set.
            # Combines all the textures into the sets [types] - if the script was able to derive a texture set from the name, e.g., "Texture_AO.png".

            else:
                derived_asset_name = "_".join(asset_name_no_ext.split("_")[:-1]) if "_" in asset_name_no_ext else asset_name_no_ext
                texture_set_name = derived_asset_name or asset_name_no_ext
                texture_id = f"{group_folder}:{texture_set_name.lower()}"
                entry = texture_sets.get(texture_id)
                if entry is None:
                    entry = SetEntry(display_name = texture_set_name, types = {}, untyped = [])
                    texture_sets[texture_id] = entry
                entry["untyped"].append(key)
            # List all remaining textures that are not part of any texture set [untyped], e.g., "Texture.png"


# Separating non-qualifying and qualifying sets:
# Collecting required maps for qualifying sets:
    available_required_maps_per_set: Dict[str, Set[str]] = {} # Stores available texture types required for packing modes per texture set.
    skipped_texture_sets: Set[str] = set()

    for texture_id, entry in texture_sets.items():
        available_tex_types: Set[str] = set(entry["types"].keys()) # Gets all available texture types for a texture set.

        if not available_tex_types:
            skipped_texture_sets.add(texture_id)
            continue
        # When a script wasn't able to derive known texture types for a texture set, or no texture set at all.

        available_required_maps: Set[str] = set() # Required texture types from qualifying modes that are actually present in this set.
        qualifies: bool = False

        for mode in valid_packing_modes:
            required_textures = _required_base_texture_map_types_for_mode(mode)
            present_textures = {b for b in required_textures if b in available_tex_types}
            if (len(required_textures) <= 2 and present_textures == required_textures) or (len(required_textures) > 2 and len(present_textures) >= 2):
                qualifies = True
                available_required_maps.update(present_textures)
        # Lets the set pass if it has at least two of this mode’s unique required maps.

        if qualifies: #
            available_required_maps_per_set[texture_id] = available_required_maps
        else:
            skipped_texture_sets.add(texture_id)


 # Building skipped set for logging summary:
    for texture_id in skipped_texture_sets:
        entry = texture_sets.get(texture_id)
        if not entry:
            continue
        group_folder, _ = texture_id.split(":", 1)
        display_set_name = entry["display_name"]

        display_files: Set[str] = set() # Set of all textures to be displayed "skipped" in logs.
        for file_list in entry["types"].values():
            for file_path in file_list:
                file_name = file_path.rsplit("/", 1)[-1]
                display_files.add(file_name)
        # For texture sets that didn't have required maps for any packing mode.

        for file_path in entry["untyped"]:
            file_name = file_path.rsplit("/", 1)[-1]
            display_files.add(file_name)
        # For textures not recognized as texture sets.

        pre_skipped_texture_sets[group_folder][display_set_name] = sorted(display_files, key=str.lower)


# Updating context selection paths to store only textures actually required for channel_packaging modes:
    selected_textures_paths: Dict[str, str] = {}
    for texture_id, required_texture_types in available_required_maps_per_set.items():
        entry = texture_sets[texture_id]
        for required_texture in required_texture_types:
            for file_path in entry["types"].get(required_texture, []):
                selected_textures_paths[file_path] = ""  # Placeholder values; dict values are absolute paths to each file - created and overridden in "prepare_workspace".
    context.selection_paths_map = selected_textures_paths


    return {relative_parent: dict(skipped_texture_sets_names) for relative_parent, skipped_texture_sets_names in pre_skipped_texture_sets.items()}
    # Returns skipped sets for logging.


def _required_base_texture_map_types_for_mode(packing_mode: "PackingMode") -> set[str]:
# Converts mapped texture types from dict into a set of required texture types.

    channels = (packing_mode.get("channels") or {})
    return {_strip_channel_specifier(channel_value).lower() for channel_value in channels.values() if channel_value}


def _present_base_texture_types_for_mode(available_maps: "TextureMapCollection", mode: "PackingMode") -> set[str]:
# Determines how many texture maps types mapped for channel pack are available.

    channels = (mode.get("channels") or {})
    base_texture_types: set[str] = set()
    for texture_map in ("R", "G", "B", "A"):
        mapped_texture_type = channels.get(texture_map)
        if not mapped_texture_type:
            continue
        base_texture_map_type = _strip_channel_specifier(mapped_texture_type).lower()
        if base_texture_map_type in available_maps:
            base_texture_types.add(base_texture_map_type)
    return base_texture_types


def _build_texture_sets(input_folder: str, initial_files: List[str], *, required_texture_types_by_set: Optional[Dict[str, Set[str]]] = None, context: Optional[CPContext] = None) -> Dict[str, TextureSet]:
# Iterates over all given files, extracts texture set name, and collects all its map data.

    raw_textures: Dict[str, TextureSet] = {}
    for file in initial_files:
        full_path: str = os.path.join(input_folder, file)
        info_from_texture_set_name = _extract_info_from_texture_set_name(full_path)
        if not info_from_texture_set_name:
            continue

        texture_set_name, texture_type, declared_suffix, original_filename = info_from_texture_set_name

        texture_set_name_lower: str = texture_set_name.lower()


        if required_texture_types_by_set is not None:
            required_textures = required_texture_types_by_set.get(texture_set_name_lower, set())
            if required_textures and texture_type.lower() not in required_textures:
                continue
        # Filters only the files that are required for a given packing mode.

        texture_resolution = _extract_image_data(full_path)

        if texture_set_name_lower not in raw_textures:
            raw_textures[texture_set_name_lower] = TextureSet(texture_set_name=texture_set_name)
            # Assigns the original texture set name extracted by a function, only for the first map, as the key doesn't exist yet.

        texture_data = TextureMapData(
            file_path=full_path,
            resolution=texture_resolution,
            suffix=declared_suffix,
            filename=original_filename,
        )

        raw_textures[texture_set_name_lower].available_texture_maps[texture_type] = texture_data
        # Creates a TextureMapData instance extracted by function and add it to the appropriate map type list.

        if context is not None and context.textures_converted_from_raw:
            normalized_full_image_path: str = os.path.abspath(full_path).replace("\\", "/")
            converted_texture: ConvertedEXRImage = context.textures_converted_from_raw.get(normalized_full_image_path)
            if converted_texture is not None:
                converted_texture.texture_set_name = texture_set_name_lower

                final_texture_type_name: str = texture_type
                for texture_type_name_from_config in TEXTURE_CONFIG.keys():
                    if texture_type_name_from_config.lower() == texture_type.lower():
                        final_texture_type_name = texture_type_name_from_config
                        break
                # Makes sure a texture type starts with a capital letter.

                converted_texture.texture_type = final_texture_type_name
        # Collects map types successfully converted from 32bit float.


    raw_textures = dict(sorted(raw_textures.items(), key=lambda kv: kv[0]))
    return raw_textures




#                                          === Mode Selection & Resolution ===

def _strip_channel_specifier(name: str) -> str:
    # Removes the channel specifier (e.g., _R, .R) from the texture name and returns the base name in lowercase.
    return re.sub(r'[._]([rgba])$', '', name, flags=re.IGNORECASE).lower()


def _extract_mode_name(packing_mode: PackingMode) -> str:
    # Extracts the packing-mode suffix from the initial letters of each texture type mapped in channels.
    # Or uses the custom suffix if present in the config.

    custom_mode_suffix: str = packing_mode.get("custom_suffix", "").strip()
    channels: ChannelMapping = packing_mode["channels"]

    if custom_mode_suffix:
        return custom_mode_suffix


    unique_map_types: Set[str] = set() # Stores only unique maps (e.g., "Normal_R", "Normal_G", "Height" > "Normal", "Height").
    derived_prefix: List[str] = []
    for channel in ["R", "G", "B", "A"]:
        mapped_texture_type: Optional[str] = channels.get(channel)
        if not mapped_texture_type:
            continue

        base_texture_map_type: str = _strip_channel_specifier(mapped_texture_type) # Removes suffixes (e.g., "_R") from the texture type name if necessary.
        first_letter: str = base_texture_map_type[0].upper()

        if base_texture_map_type not in unique_map_types:
            derived_prefix.append(first_letter)
            unique_map_types.add(base_texture_map_type)
        # Adds only the first letters of unique texture types.
    return "".join(derived_prefix)


def _get_available_texture_maps_for_packing(mode: PackingMode, available_maps: TextureMapCollection) -> TextureMapCollection:
    # Returns a dict of available map types used in each PackingMode.
    # If a set contains the same map type in multiple resolutions, picks the highest for packing.
    channels = (mode.get("channels") or {})
    available_texture_maps: TextureMapCollection = {}
    unique_texture_types: set[str] = set()

    for channel_ in ("R", "G", "B", "A"):
        channel = channels.get(channel_)
        if not channel:
            continue
        base_texture_map_type = _strip_channel_specifier(channel)  # Lowercase, removes e.g., "_R" for RGB maps.
        if base_texture_map_type in unique_texture_types:
            continue
        if base_texture_map_type in available_maps:
            texture_maps_for_type = available_maps[base_texture_map_type]
            if isinstance(texture_maps_for_type, list):
                max_resolution_texture = max(texture_maps_for_type, key=lambda t: (t.resolution or (0,0))[0] * (t.resolution or (0,0))[1])
            else:
                max_resolution_texture = texture_maps_for_type
            available_texture_maps[base_texture_map_type] = max_resolution_texture # Picks the largest resolution for the same map type.
            unique_texture_types.add(base_texture_map_type)
    return available_texture_maps


def _get_valid_modes_for_set(original_name: str, available_maps: TextureMapCollection, valid_packing_modes: List[PackingMode]) -> List[ValidModeEntry]:
# Adds only the texture maps used to a packing mode that expects them and also adds its case-sensitive texture set name for easier identification of a processed set during generation of the channel-packed image.
# Requires at least 2 maps for a packing mode.
    valid_modes_with_maps: List[ValidModeEntry] = []
    available_texture_types = set(available_maps.keys())

    for mode in valid_packing_modes:
        required_texture_types = _required_base_texture_map_types_for_mode(mode)
        present_texture_types = required_texture_types & available_texture_types


        if (len(required_texture_types) <= 2 and present_texture_types != required_texture_types) or (len(required_texture_types) > 2 and len(present_texture_types) < 2):
            continue

        texture_maps_for_mode: TextureMapCollection = _get_available_texture_maps_for_packing(mode, available_maps)
        if len(texture_maps_for_mode) < 2:
            continue

        valid_modes_with_maps.append(
            ValidModeEntry(
                texture_set_name=original_name,
                mode=mode,
                texture_maps_for_mode=texture_maps_for_mode,
                packing_mode_suffix=_extract_mode_name(mode)
            )
        )
    return valid_modes_with_maps


def _check_textures_and_pick_target_resolution(
    texture_maps_for_mode: TextureMapCollection,
    resize_strategy: str,
    packing_mode_name: Optional[str] = None,
) -> Tuple[bool, Tuple[int, int]]:
# Checks resolution of all textures required by a packing mode, picks one according to the resize strategy set in .JSON case of res mismatch.
# Also skips packing mode for a set if it encounters texture resolution that is not 2^n mip-map friendly, and returns errors to be logged in summarize_mode_results

    mode_name = packing_mode_name or ""

    texture_resolutions: List[Tuple[int, int]] = []
    texture_count = len(texture_maps_for_mode)
    for texture in texture_maps_for_mode.values():
        if texture.resolution is not None:
            texture_resolutions.append(texture.resolution)
    # Lists all resolutions.


    if not texture_resolutions or len(texture_resolutions) < texture_count:
        return False, (0, 0)
    # Skips mode if files are corrupted.


    min_resolution = min(texture_resolutions, key=lambda r: r[0] * r[1])
    max_resolution = max(texture_resolutions, key=lambda r: r[0] * r[1])


    if not is_power_of_two(min_resolution[0]) or not is_power_of_two(min_resolution[1]):
        return False, min_resolution
    # Skips a texture set if any texture doesn't have 2^n resolution.


    if len(set(texture_resolutions)) == 1:
        return True, min_resolution
    # When all textures have the same resolution.


    if resize_strategy == "up":
        return True, max_resolution
    else:
        return True, min_resolution
    # In case of resolution mismatch.


def _list_textures_to_scale(
    maps_for_mode: TextureMapCollection,
    target_size: Tuple[int, int]
) -> List[MapNameAndResolution]:
# Lists all filenames and their resolutions for texture maps that are mismatched and need to be scaled.

    textures_to_scale: List[MapNameAndResolution] = []
    for texture in maps_for_mode.values():
        if texture.resolution != target_size:
            textures_to_scale.append(MapNameAndResolution(texture.filename, texture.resolution))
    return textures_to_scale




#                                        === Warnings / Logging ===

def _check_suffix_warnings_for_set(maps_for_mode: TextureMapCollection) -> List[MapNameAndResolution]:
# Iterates over all textures required by a packing mode to check whether there is a mismatch between the declared size in the name (if given) and the actual file resolution.

    warnings: List[MapNameAndResolution] = []
    for texture in maps_for_mode.values():
        suffix_warning = check_texture_suffix_mismatch(texture)
        if suffix_warning:
            warnings.append(suffix_warning)
    return warnings


def _list_missing_texture_maps_for_channel_mapping(channel_mapping: ChannelMapping, maps_for_mode: TextureMapCollection) -> List[str]:
# Lists all textures that are missing for a packing mode to be displayed in logs.
    available_texture_maps_lower: Set[str] = {k.lower() for k in maps_for_mode.keys()}
    missing_texture_maps: List[str] = []
    for mapped_texture_type in channel_mapping.values():
        if not mapped_texture_type:
            continue
        base_texture_map_type: str = mapped_texture_type.split(".")[0].lower()
        if base_texture_map_type not in available_texture_maps_lower:
            missing_texture_maps.append(base_texture_map_type)
    return missing_texture_maps


def _print_warnings(warning_items: Union[List[MapNameAndResolution], List[str]],
                    warning_displayed: bool,
                    warning_type: str,  # "resolution" | "suffix" | "missing_maps" | "exr_source"
                    *,
                    target_resolution: Optional[Tuple[int, int]] = None,  # only for "resolution"
                    packing_mode_name: Optional[str] = None,  # Only for "missing_maps"
                    ) -> bool:

    if not warning_items:
        return warning_displayed

    if warning_type == "resolution":
        simplified = "Texture set resolution mismatch."
        detailed = f"Texture set resolution mismatch:\n   Resize strategy set to '{RESIZE_STRATEGY}'"
    elif warning_type == "suffix":
        simplified = "Suffix resolution mismatch."
        detailed = "Suffix resolution mismatch:"
    elif warning_type == "missing_maps":
        packing_mode_name_ = packing_mode_name or ""
        simplified = f"Missing some texture maps for '{packing_mode_name_}'."
        detailed = f"Missing texture map for '{packing_mode_name_}':"
    elif warning_type == "exr_source":
        simplified = "Float image source detected."
        detailed = "Float image source detected:"
    else:
        simplified = detailed = "Warning."
    # Chooses a warning type to print.


    if not warning_displayed:
        log(detailed if SHOW_DETAILS else simplified, "warn")
        # Prints warning.
        warning_displayed = True
    # Displays the main warning type only once per texture set.

    if SHOW_DETAILS:
        if warning_type == "resolution":
            if target_resolution:
                target_width, target_height = target_resolution
                for warning_entry in warning_items:
                    width, height = warning_entry.resolution
                    log(f"Rescaling {warning_entry.filename} ({width}x{height}) to {target_width}x{target_height}", "info")
                    # Prints info.
        elif warning_type == "suffix":
            for warning_entry in warning_items:
                width, height = warning_entry.resolution
                log(f"{warning_entry.filename} but it's {width}x{height}", "info")
                # Prints info.
        elif warning_type == "missing_maps":
            for miss in warning_items:
                for original_key in TEXTURE_CONFIG.keys():
                    if original_key.lower() == miss:
                        log(f"Default value: {original_key}", "info")
                        # Prints info.
                        break
        elif warning_type == "exr_source":
            for texture in warning_items:
                log(f"Converted: {texture}", "info")
                # Prints info.

    # Prints warnings for each affected file if SHOW_DETAILS was set to true.
    return warning_displayed




#                                              === Generation ===

def _extract_channel(image: Optional[ImageObject], texture_map_type: str) -> Optional[ImageObject]:
# Extracts the channel specified by the packing mode from an RGB/RGBA image. E.g., B: Normal_R. > Normal red channel
    if image is None:
        return None

# Preparing grayscale:
    image_mode: str = get_image_mode(image)
    if is_grayscale(image):
        return convert_to_grayscale(image)
    # If the image type is 8bit grayscale, returns it as-is, otherwise converts the grayscale from 16 to 8 bit.

    if image_mode in ("RGB", "RGBA"):
# Preparing RGB images with a specified channel:
        channel_match = re.search(r'[._]([rgba])$', texture_map_type, re.IGNORECASE)
        requested_channel: str = channel_match.group(1).upper() if channel_match else ""
        # Derives the texture type name from PACKING_MODE channel values (e.g., Normal_R).

        image_channels = get_image_channels(image)
        if requested_channel and requested_channel in image_channels:
            return get_channel(image, requested_channel)
        # If the image is RGB/RGBA and the requested channel is valid, extracts that channel.


# Preparing grayscale images saved as RGB:
        base_texture_type: str = texture_map_type.split(".", 1)[0].lower()
        is_texture_grayscale: bool = False
        for texture_type, config in TEXTURE_CONFIG.items():
            if texture_type.lower() == base_texture_type:
                is_texture_grayscale = (config.get("default", ("G", 0))[0] or "").upper() == "G"
                break
        # Checks if the set map type should be single channel data only, e.g., "AO".


        if is_texture_grayscale and image_mode in ("RGB", "RGBA") and "R" in image_channels and "G" in image_channels:
            try:
                if is_rgb_grayscale(image):
                    return get_channel(image, "R")
            except Exception:
                pass
        #  Checks if RGB texture is just a grayscale image saved as RGB instead of L.

        return convert_to_grayscale(image)
        # Otherwise, converts the image to grayscale as a fallback.
    else:
        log(f"Unsupported image mode '{image_mode}' for '{texture_map_type}'.", "error")
        return None


def _generate_channel_packed_texture(
    valid_packing_mode_entry: ValidModeEntry, # Original name - mode (name, custom_suffix, channels) - maps used for this mode (tex type: [(path, resolution=, suffix, filename, ext)]).
    target_resolution: Dict[str, Tuple[int, int]], # Final resolution for each mode that files are going to be generated to, according to RESIZE_STRATEGY from config.
    target_directory: str, # Absolute path to a folder where textures are generated.
    backup_directory: Optional[str] = None, # Absolute path to a folder where textures used for generation are moved afterward.
    context: Optional[CPContext] = None
) -> Optional[str]: # Returns the file name of created map and texture types that needed to be generated in case of missing.

    packing_mode: PackingMode = valid_packing_mode_entry.mode # Packing mode that is valid due to check for necessary maps earlier.
    packing_mode_name: str = packing_mode["mode_name"].strip() # Name of packing mode e.g., ARM.
    texture_maps_for_mode: TextureMapCollection = valid_packing_mode_entry.texture_maps_for_mode # Only maps that are required by the current packing mode and their corresponding data (tex type: [(path, resolution=, suffix, filename, ext)]).
    target_resolution: tuple[int, int] = target_resolution.get(packing_mode_name, (0, 0)) # Setting target resolution for all files during generation.
    missing_texture_maps: List[str] = [] # Lists all texture's set missing maps required for a given packing mode.
    loaded_textures: Dict[str, Optional[ImageObject]] = {} # Stores loaded texture maps by type, e.g., {"Albedo": <PIL.Image.Image image mode=L size=2048x2048>, "Normal": <PIL.Image.Image image mode=RGB size=2048x2048>, "Roughness": None}.
    channels: List[ImageObject] = [] # List of all images collected to generate the final image.
    packed_texture: Optional[ImageObject] = None # Final generated image.



# Preparing an output file type:
    channels_config = cast(Dict[str, str], packing_mode["channels"]) # Variable cast due to TypedDict > Dict issue
    generated_image_mode: str = "RGBA" if channels_config.get("A") else "RGB" # Decides whether the output image should have 3 or 4 channels,
    output_channel_mapping: List[Tuple[str, str]] = [(c, channels_config[c]) for c in generated_image_mode] # Builds a list of (channel, texture_name) mappings for the output image.

    # Output file type


# Loading texture maps:
    try:
        for texture_name, texture_data in texture_maps_for_mode.items():
            try:
                texture = open_image(texture_data.file_path)
                if get_size(texture) != target_resolution:
                    texture_resized = resize(texture, target_resolution)
                    close_image(texture)
                    texture = texture_resized
                loaded_textures[texture_name] = texture # Maps image data to corresponding a texture name.
            except (OSError, ValueError) as e:
                log(f"Warning: failed to open '{texture_data.file_path}' ({e}), will use default.", "warn")
                # Prints warning.
                loaded_textures[texture_name] = None


# Scaling all textures to the target size (if mismatched resolutions) and filling in default values if missing any texture maps:
        for texture_name in texture_maps_for_mode.keys():
            target_texture: Optional[ImageObject] = loaded_textures.get(texture_name)
            if target_texture is None:
                default_map_value: int
                _ , default_map_value = TEXTURE_CONFIG[texture_name.lower()]["default"] # From the tuple (G/RGB, int), takes only the default fill value.
                loaded_textures[texture_name] = new_image_grayscale(target_resolution, default_map_value)
                missing_texture_maps.append(texture_name)
            # Gets default values for each map type from config and creates a missing map for packing if necessary.
            elif get_size(target_texture) != target_resolution:
                loaded_textures[texture_name] = resize(target_texture, target_resolution)
            # Scales all textures to the target size, if mismatched resolutions.


# Collecting images for each final image channel:
        for _, texture_map_name in output_channel_mapping:
            base_texture_type: str = texture_map_name.split(".")[0].lower()
            texture: ImageObject = loaded_textures.get(base_texture_type)

            if texture is None:
                default_map_value: int = 128  # Default fallback, needed for type validation.
                for texture_type, texture_config in TEXTURE_CONFIG.items():
                    if texture_type.lower() == base_texture_type:
                        default_map_value: int = texture_config["default"][1] # Uses default fill value of a corresponding map, e.g., ("RGB", 128)
                        break
                texture = new_image_grayscale(target_resolution, default_map_value)
                missing_texture_maps.append(base_texture_type)
            # Creates maps with derived default values if missing; case-insensitive.

            mapped_texture_type: ImageObject = _extract_channel(texture, texture_map_name) # Passes a chosen texture map if grayscale, if RGB, then extracts specific channel, derived from .R .G .B in its name.
            channels.append(mapped_texture_type)


        # Generating the final image:
        packed_texture = merge_channels(generated_image_mode, channels)


# Saving the file:
        display_name: str = valid_packing_mode_entry.texture_set_name # Case-sensitive texture set name, e.g., "Wall".
        packing_mode_suffix: str = valid_packing_mode_entry.packing_mode_suffix.strip()
        resolution_suffix: str = (f"_{resolution_to_suffix(target_resolution)}" if any(tex.suffix for tex in texture_maps_for_mode.values()) else "") # Only if the original file name also has size suffix.
        filename: str = f"{display_name}_{packing_mode_suffix}{resolution_suffix}"

        save_generated_texture(packed_texture, target_directory, filename, packing_mode_name, context)


        if backup_directory:
            for texture_data in texture_maps_for_mode.values():
                move_used_map(texture_data.file_path, backup_directory, context)
        return filename


    finally:
        handles_to_close = list(loaded_textures.values()) + channels
        if packed_texture is not None:
            handles_to_close.append(packed_texture)
        close_image_files(handles_to_close)
    # Safely closes all opened images even if there is an error during image processing. The wrapper has no context manager, otherwise the file handle stays open.




#                                        === Reporting / Summary ===

def _summarize_mode_results(
    original_texture_set_name: str,
    valid_packing_modes: List[PackingMode],
    valid_packing_modes_with_maps: List[ValidModeEntry],
    target_texture_resolution: Dict[str, Tuple[int, int]],
    *,
    invalid_packing_modes: Optional[Set[str]] = None,
    invalid_packing_mode_dimensions: Optional[Dict[str, Tuple[int, int]]] = None,
    context: Optional[CPContext] = None,
    log_prefix: str = ""
) -> None:
# Summarizes all Packing Modes for a given texture set – lists skipped and completed modes.

    invalid_mode_names: Set[str] = invalid_packing_modes or set()
    invalid_dimensions: Dict[str, Tuple[int, int]] = invalid_packing_mode_dimensions or {}


    for mode in valid_packing_modes:
        mode_name: str = mode["mode_name"]

        if mode_name in invalid_mode_names:
            width, height = invalid_dimensions.get(mode_name, (0, 0))
            if (width, height) == (0, 0):
                log(f"{log_prefix} '{mode_name}' Corrupted files missing resolution info - skipping mode.", "error")
                # Prints error.

            else:
                if SHOW_DETAILS:
                    log(f"{log_prefix} '{mode_name}' Invalid resolution ({width}x{height}) - skipping mode.", "error")
                else:
                    log(f"{log_prefix} '{mode_name}' Invalid resolution - skipping mode.", "error")
                # Prints error.
            continue

        valid_packing_mode = next((mode for mode in valid_packing_modes_with_maps if mode.mode["mode_name"] == mode_name), None)
        # Skips invalid modes, so they are not logged again in the summary,

        if valid_packing_mode:
            file_extension: str = context.export_extension
            filename = f"{original_texture_set_name}_{valid_packing_mode.packing_mode_suffix}.{file_extension}"
            target_resolution = target_texture_resolution.get(mode_name, (0, 0))
            if target_resolution != (0, 0):
                if SHOW_DETAILS:
                    target_width, target_height = target_resolution
                    log(f"{log_prefix} Created: {filename} ({target_width}x{target_height})", "complete")
                    #  Prints completed.
                else:
                    log(f"{log_prefix} Created: {filename}", "complete")
                    #  Prints completed.
        else:
            if SHOW_DETAILS:
                log(f"{log_prefix} Skipped: '{mode_name}' for set '{original_texture_set_name}' (needs at least 2 required maps).", "warn")
            else:
                log(f"{log_prefix} Skipped: '{mode_name}' for set '{original_texture_set_name}'", "warn")
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