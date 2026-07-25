from enum import Enum


class VideoExportQuality(Enum):
    LOW = 1
    NORMAL = 2
    HIGH = 3
    INDISTINGUISHABLE = 4
    NEAR_LOSSLESS = 5
    LOSSLESS = 6


def get_crf_for_quality(quality: VideoExportQuality) -> int:
    return {
        VideoExportQuality.LOW: 23,
        VideoExportQuality.NORMAL: 18,
        VideoExportQuality.HIGH: 14,
        VideoExportQuality.INDISTINGUISHABLE: 8,
        VideoExportQuality.NEAR_LOSSLESS: 3,
        VideoExportQuality.LOSSLESS: 0,
    }[quality]
