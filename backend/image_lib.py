
""" Image processing backend. Currently implemented using Pillow (PIL). PIL exports 8bit images only."""



#                                           === Backend ===

from array import array
from typing import Any, Sequence, Tuple, TypeAlias

from PIL import Image as _PIL
from PIL.Image import Image as PILImage
from PIL import Image as PILImageModule
from PIL import ImageChops

ImageObject: TypeAlias = PILImage


def close_image(image: object) -> None:
    close = getattr(image, "close", None)
    if callable(close):
        close()


def from_array_u8(data: Any, mode: str) -> ImageObject:
# Creates an image from a uint8 numpy array.
    return PILImageModule.fromarray(data, mode)


def get_channel(image: ImageObject, ch: str) -> ImageObject:
# Extracts a single channel by name ("R","G","B","A","L")
    return image.getchannel(ch.upper())


def get_image_channels(image: ImageObject) -> Tuple[str, ...]:
# Returns the channel names for an already open image.
# Pillow: ("R","G","B"), ("R","G","B","A"), ("L",)
    return image.getbands()


def get_image_mode(image: Any) -> str:
# Return the Pillow image mode: "RGB", "RGBA", "L"
    return image.mode


def get_size(image: ImageObject) -> Tuple[int, int]:
# Returns the image size as (width, height)
    return image.size


def merge_channels(mode: str, channels: Sequence[Any]) -> ImageObject:
# Merge separate channels into a single image.
    return _PIL.merge(mode, tuple(channels))


def new_image_grayscale(size: Tuple[int, int], fill: int) -> Any:
# Create a new grayscale image.
    return _PIL.new("L", size, fill)


def open_image(path: str) -> ImageObject:
    return _PIL.open(path)


def resize(image: ImageObject, size: Tuple[int, int]) -> ImageObject:
# Resize an image using bilinear resampling.
    return image.resize(size, _PIL.BILINEAR)


def save_image(image: Any, path: str) -> None:
    image.save(path)




#                                           === Utils ===



def is_grayscale(image: ImageObject) -> bool:
# Returns True if the image is of type grayscale image.

    mode = get_image_mode(image)
    return mode in ("L", "LA") or mode == "I" or str(mode).startswith("I;16")


def are_channels_equal(image: ImageObject, input_channel1: str, input_channel2: str) -> bool:
# Returns True if both channels are identical.

    try:
        channel1 = get_channel(image, input_channel1)
        channel2 = get_channel(image, input_channel2)
        return ImageChops.difference(channel1, channel2).getbbox() is None
    except Exception:
        return False


def is_rgb_grayscale(image: ImageObject) -> bool:
# Checks if RGB texture is just a grayscale image saved as RGB instead of L.

    try:
        ext = image.getextrema()  # (Rmin,Rmax),(Gmin,Gmax),(Bmin,Rmax),(Amin,Amax)
        if not ext or len(ext) < 2 or ext[0] != ext[1]:
            return False
        # Pre-validation: checks if the channels extremes are the same.

    except Exception:
        pass
    return are_channels_equal(image, "R", "G")


def convert_to_grayscale(image: ImageObject) -> ImageObject:
# Converts an image to 8-bit grayscale.
    mode = image.mode
    if mode == "L":
        return image
    if mode in ("I", "I;16", "I;16L", "I;16B"):
        return _16_to_8bit(image)
    return image.convert("L")


def _16_to_8bit(image: ImageObject) -> ImageObject:
# Scales down 16bit range to a 8bit, so values are properly maintained instead of being clipped.

# Preparing the image:
    if image.mode == "I":
        img16 = image.convert("I;16")
    elif image.mode in ("I;16", "I;16L", "I;16B"):
        img16 = image if image.mode == "I;16" else image.convert("I;16")
    # Normalizes the image type to 16bit LE.
    else:
        return image.convert("L")
    # If the image is just 8bit grayscale, passes it though.

    raw = img16.tobytes("raw", "I;16")  # LE 16bit
    data16 = array("H")
    data16.frombytes(raw)

# Scaling:
    data8 = bytearray((v >> 8) & 0xFF for v in data16)
    return PILImageModule.frombytes("L", img16.size, bytes(data8))