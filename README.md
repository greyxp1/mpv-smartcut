# mpv-smartcut

`mpv-smartcut` is a small mpv frontend and maintained packaging fork of
[skeskinen/smartcut](https://github.com/skeskinen/smartcut). It makes
frame-accurate cuts using stream copying and near-lossless boundary encoding.

The frontend and backend ship together but remain separate processes:

- `mpv-smartcut.lua` owns selection, progress, cancellation, temporary files,
  cleanup, and completion messages.
- `mpv-smartcut-backend` is the minimal codec-aware worker invoked by the
  frontend and publishes completed cuts without overwriting existing files.

The Nix package binds its installed script to the matching backend and can be
used directly in mpv's script list.

The fork preserves the original MIT license. It streams the selected media in
one pass and buffers neighboring GOPs for frame-accurate boundaries, without
scanning the whole file or loading its audio into memory first.

## mpv usage

Copy `mpv-smartcut.lua` to mpv's `scripts` directory and ensure
`mpv-smartcut-backend` is in `PATH`. Press `c` at both ends of the desired
range. Press `C` to clear a selection or cancel a running cut.

Optional settings belong in `script-opts/mpv-smartcut.conf`:

```ini
backend=mpv-smartcut-backend
cut_key=c
cancel_key=C
output_prefix=cut_
```

Only the small boundary GOPs around each cut are re-encoded, at near-lossless
quality so they blend with the copied video. H.264, HEVC, VP8, VP9, and AV1 use
fast software encoder settings for these boundaries, trading larger boundary
GOPs for speed. HEVC and AV1 boundaries with 4:2:0 video use
NVENC when an NVIDIA GPU supports it, which is about 10 to 20 times faster than
software at equal quality.
Codec threading is bounded, and decoded frames are consumed directly to limit
memory use during playback.
Independent complete GOPs are copied unchanged. H.264 open GOPs may require
re-encoding until the next independent keyframe. Encoding speed still depends
on the codec, resolution, and distance to the nearest keyframes.
Audio-only files with unknown track durations may need to be read to EOF.
Older MPEG encoders use their finest quantizer instead.

Cuts are written to a unique hidden partial file and published only after success.
Partial output is deleted when processing fails, is cancelled, or mpv exits.
If publication fails, including when the final filename becomes occupied, the
completed partial file is kept and its path is shown.
