import argparse
import os
import time
from fractions import Fraction

from smartcut.media_container import MediaContainer
from smartcut.media_utils import VideoExportQuality
from smartcut.smart_cut import smart_cut

QUALITY_PRESETS = {
    "low": VideoExportQuality.LOW,
    "normal": VideoExportQuality.NORMAL,
    "high": VideoExportQuality.HIGH,
    "indistinguishable": VideoExportQuality.INDISTINGUISHABLE,
    "near-lossless": VideoExportQuality.NEAR_LOSSLESS,
    "lossless": VideoExportQuality.LOSSLESS,
}


def parse_range(value: str) -> tuple[Fraction, Fraction]:
    try:
        start_text, end_text = value.split(",")
        start = Fraction(start_text)
        end = Fraction(end_text)
    except (ValueError, ZeroDivisionError) as error:
        raise argparse.ArgumentTypeError("range must be START,END in seconds") from error

    if start < 0 or end <= start:
        raise argparse.ArgumentTypeError("range must satisfy 0 <= START < END")
    return start, end


class Progress:
    def __init__(self, path: str) -> None:
        self.path = path
        self.last_write = 0.0

    def emit(self, completed: int, total: int) -> None:
        now = time.monotonic()
        if completed < total and now - self.last_write < 0.1:
            return

        temporary_path = f"{self.path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as progress:
            progress.write(f"{completed},{total}\n")
        os.replace(temporary_path, self.path)
        self.last_write = now


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--keep", required=True, type=parse_range)
    parser.add_argument("--progress-file", required=True)
    parser.add_argument("--quality", required=True, choices=QUALITY_PRESETS)
    args = parser.parse_args()

    source = MediaContainer(args.input)
    try:
        smart_cut(
            source,
            args.keep,
            args.output,
            QUALITY_PRESETS[args.quality],
            Progress(args.progress_file),
        )
    finally:
        source.close()


if __name__ == "__main__":
    main()
