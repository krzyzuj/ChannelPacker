# Channel Packer (Standalone)

[![ArtStation - krzyzuj](https://img.shields.io/badge/ArtStation-krzyzuj-blue?logo=artstation)](https://artstation.com/krzyzuj)

A standalone Python tool that automates texture channel packing. Set up the config once, point it at a working folder.
It will gather the required maps, validate them, and pack channels according to your presets, significantly speeding up the workflow.
Tested on Windows.

> Note: Originally a single module with a shared backend for both the standalone version and Unreal Engine.
It was later split for ease of use, but the core structure remains the same, so changes are drop-in across versions.


## Features
- Multiple packing modes defined in the config let you generate various texture combinations in a single pass.
- Automatic organization: moves created and/or source maps into subdirectories to keep things tidy.
- Flexible inputs: supports packing grayscale textures as well as extracting specific channels from RGB sources.
- Validation & logging: checks for resolution mismatches, incorrect filenames, and missing maps, and logs any issues it finds.
- Auto-repair options: can fill missing channels with default values and rescale mismatched textures when needed.

## Usage
Configure your packing settings in config.json.
Run the channel_packer module from the CLI.
The working directory can be set in the config or passed as an argument.

## Requirements
Channel Packer requires Python 3.11 with [Pillow](https://pillow.readthedocs.io/en/stable/index.html) 11.3 to run.

## Config
&NewLine;

| mandatory | label | input type | description | if empty 
|-----------|------------|-------------|-----------|-----------|
| yes | input_folder | folder path | path to the source textures; can be overridden via a CLI argument | x |
| yes | file_type | file ext | file type extension for the created texture files | png |
| no | delete_used | true/false | deletes used source files after packing | false |
| no | resize_strategy | up/down | resolves resolution mismatches within a set, by scaling the textures up or down | down |
| no | backup_folder_name | folder name | moves used files into this subfolder after packing | - |
| no | custom_folder_name | folder name | saves generated textures into this subfolder | - |
| yes | mode_name | mode id | must not be empty to be considered by the function | x |
| no |custom_suffix | suffix name | custom suffix for the created textures | auto|
| yes |channels | texture map types | textures mapped to each channel of the final generated texture; alpha can be left empty | x |
