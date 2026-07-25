import os
import sys
import time
from fractions import Fraction

from smartcut.media_container import MediaContainer
from smartcut.smart_cut import smart_cut

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
    input_path, output_path, start, end, progress_path, quality = sys.argv[1:]
    source = MediaContainer(input_path)
    try:
        smart_cut(
            source,
            (Fraction(start), Fraction(end)),
            output_path,
            quality,
            Progress(progress_path),
        )
    finally:
        source.close()


if __name__ == "__main__":
    main()
