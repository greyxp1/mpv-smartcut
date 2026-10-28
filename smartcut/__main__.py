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
    input_path, output_path, start, end, progress_path, quality, final_path = sys.argv[1:]
    selection = (Fraction(start), Fraction(end))
    source = MediaContainer(input_path, selection)
    try:
        with open(output_path, "xb") as output:
            smart_cut(
                source,
                selection,
                output,
                quality,
                Progress(progress_path),
            )
    finally:
        source.close()

    try:
        os.link(output_path, final_path)
        os.unlink(output_path)
    except OSError as error:
        print(f"Could not finalize cut: {error}. Completed cut kept at {output_path}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
