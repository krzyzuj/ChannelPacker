
""" Image processing backend for channel_packer. Currently implemented using Pillow (PIL). """

from typing import Any, Sequence, Tuple, TypeAlias

from PIL import Image as _PIL
from PIL.Image import Image as PILImage

ImageObj: TypeAlias = PILImage


def close_image(im: object) -> None:
    close = getattr(im, "close", None)
    if callable(close):
        close()


def get_bands(im: ImageObj) -> Tuple[str, ...]:
# Returns the channel names for an already open image.
# Pillow: ("R","G","B"), ("R","G","B","A"), ("L",)
    return im.getbands()


def get_channel(im: ImageObj, ch: str) -> ImageObj:
# Extracts a single channel by name ("R","G","B","A","L")
    return im.getchannel(ch.upper())


def get_mode(im: Any) -> str:
# Return the Pillow image mode: "RGB", "RGBA", "L"
    return im.mode


def get_size(im: ImageObj) -> Tuple[int, int]:
# Returns the image size as (width, height)
    return im.size


def merge_channels(mode: str, channels: Sequence[Any]) -> ImageObj:
# Merge separate channels into a single image.
    return _PIL.merge(mode, tuple(channels))


def new_gray(size: Tuple[int, int], fill: int) -> Any:
# Create a new grayscale image.
    return _PIL.new("L", size, fill)


def open_image(path: str) -> ImageObj:
    return _PIL.open(path)


def resize(im: ImageObj, size: Tuple[int, int]) -> ImageObj:
# Resize an image using bilinear resampling.
    return im.resize(size, _PIL.BILINEAR)


def save_image(im: Any, path: str) -> None:
    im.save(path)


def to_grayscale(im: ImageObj) -> ImageObj:
# Convert an image to 8-bit grayscale ('L' mode):
    return im.convert("L")