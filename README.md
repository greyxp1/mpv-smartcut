# mpv-smartcut

Frame-accurate cutting for mpv, forked from
[skeskinen/smartcut](https://github.com/skeskinen/smartcut). Complete GOPs are
copied unchanged and only the GOPs around each cut point are re-encoded, at
near-lossless quality so they blend with the rest of the clip.

## Usage

Copy `mpv-smartcut.lua` to mpv's `scripts` directory and put
`mpv-smartcut-backend` in `PATH`. The Nix package's script already points to its
backend and can go straight into mpv's script list.

Press `c` at both ends of a range to cut it next to the source file. Press `C`
to clear the selection or cancel a running cut.

Defaults can be changed in `script-opts/mpv-smartcut.conf`:

```ini
backend=mpv-smartcut-backend
cut_key=c
cancel_key=C
output_prefix=cut_
```

## Notes

HEVC and AV1 boundaries use NVENC on NVIDIA GPUs, which is 10 to 20 times
faster than the software encoders used otherwise.

H.264 open GOPs may need re-encoding until the next IDR frame. Files with an
audio track that ends before the cut may be read to the end.

Existing files are never overwritten. A cut is written to a hidden directory
next to the source and published with a hard link. If the output name is taken
by then, or the filesystem has no hard links (FAT and some network mounts), the
completed cut stays in the hidden directory and its path is shown.
