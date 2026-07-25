"""Video export modes and quality presets."""

from enum import Enum


class VideoExportMode(Enum):
    SMARTCUT = 1
    KEYFRAMES = 2
    RECODE = 3


class VideoExportQuality(Enum):
    LOW = 1
    NORMAL = 2
    HIGH = 3
    INDISTINGUISHABLE = 4
    NEAR_LOSSLESS = 5
    LOSSLESS = 6


def get_crf_for_quality(quality: VideoExportQuality) -> int:
    """Return the CRF for a quality preset."""
    return {
        VideoExportQuality.LOW: 23,
        VideoExportQuality.NORMAL: 18,
        VideoExportQuality.HIGH: 14,
        VideoExportQuality.INDISTINGUISHABLE: 8,
        VideoExportQuality.NEAR_LOSSLESS: 3,
        VideoExportQuality.LOSSLESS: 0,
    }.get(quality, 18)
