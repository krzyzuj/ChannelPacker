
""" Image processing backend. Currently implemented using Pillow (PIL). PIL exports 8bit images only."""



#                                           === Backend ===

from array import array
from typing import Any, Sequence, Tuple, TypeAlias

from PIL import Image as _PIL
from PIL.Image import Image as PILImage
from PIL import Image as PILImageModule
from PIL import ImageChops

ImageObj: TypeAlias = PILImage


def close_image(im: object) -> None:
    close = getattr(im, "close", None)
    if callable(close):
        close()


def from_array_u8(data: Any, mode: str) -> ImageObj:
# Creates an image from a uint8 numpy array.
    return PILImageModule.fromarray(data, mode)


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




#                                           === Utils ===



def is_grayscale(im: ImageObj) -> bool:
# Returns True if the image is of type grayscale image.

    m = get_mode(im)
    return m in ("L", "LA") or m == "I" or str(m).startswith("I;16")


def are_channels_equal(im: ImageObj, ch1: str, ch2: str) -> bool:
# Returns True if both channels are identical.

    try:
        c1 = get_channel(im, ch1)
        c2 = get_channel(im, ch2)
        return ImageChops.difference(c1, c2).getbbox() is None
    except Exception:
        return False


def is_rgb_grayscale(img: ImageObj) -> bool:
# Checks if RGB texture is just a grayscale image saved as RGB instead of L.

    try:
        ext = img.getextrema()  # (Rmin,Rmax),(Gmin,Gmax),(Bmin,Rmax),(Amin,Amax)
        if not ext or len(ext) < 2 or ext[0] != ext[1]:
            return False
        # Pre-validation: checks if the channels extremes are the same.

    except Exception:
        pass
    return are_channels_equal(img, "R", "G")


def to_grayscale(img: ImageObj) -> ImageObj:
# Converts an image to 8-bit grayscale.
    mode = img.mode
    if mode == "L":
        return img
    if mode in ("I", "I;16", "I;16L", "I;16B"):
        return _16_to_8bit(img)
    return img.convert("L")


def _16_to_8bit(img: ImageObj) -> ImageObj:
# Scales down 16bit range to a 8bit, so values are properly maintained instead of being clipped.

# Preparing the image:
    if img.mode == "I":
        img16 = img.convert("I;16")
    elif img.mode in ("I;16", "I;16L", "I;16B"):
        img16 = img if img.mode == "I;16" else img.convert("I;16")
    # Normalizes the image type to 16bit LE.
    else:
        return img.convert("L")
    # If the image is just 8bit grayscale, passes it though.

    raw = img16.tobytes("raw", "I;16")  # LE 16bit
    data16 = array("H")
    data16.frombytes(raw)

# Scaling:
    data8 = bytearray((v >> 8) & 0xFF for v in data16)
    return PILImageModule.frombytes("L", img16.size, bytes(data8))