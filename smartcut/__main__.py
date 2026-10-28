import os
import shutil
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


def remove_if_killed(job_path: str) -> int:
    """Fork a watcher that removes the job directory if the backend dies between
    writing b"c" (created) and b"d" (done) to the returned fd."""
    read_end, write_end = os.pipe()
    if os.fork() == 0:
        os.close(write_end)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        if os.read(read_end, 1) and not os.read(read_end, 1):
            shutil.rmtree(job_path, ignore_errors=True)
        os._exit(0)
    os.close(read_end)
    return write_end


def main() -> None:
    input_path, start, end, job_path, final_path = sys.argv[1:]
    # Fork first so a kill right after mkdir is still cleaned up.
    watcher = remove_if_killed(job_path)
    os.mkdir(job_path, 0o700)
    os.write(watcher, b"c")
    output_path = os.path.join(job_path, os.path.basename(final_path))
    progress_path = os.path.join(job_path, "progress")
    keep = False
    try:
        selection = (Fraction(start), Fraction(end))
        source = MediaContainer(input_path, selection)
        try:
            with open(output_path, "xb") as output:
                smart_cut(source, selection, output, Progress(progress_path))
        finally:
            source.close()

        try:
            os.link(output_path, final_path)
        except OSError as error:
            keep = True
            os.remove(progress_path)
            print(f"Could not publish cut: {error}. Completed cut kept at {output_path}", file=sys.stderr)
            sys.exit(2)
    finally:
        if not keep:
            shutil.rmtree(job_path)
        os.write(watcher, b"d")


if __name__ == "__main__":
    main()
