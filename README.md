# mpv-smartcut

`mpv-smartcut` is a small mpv frontend and maintained packaging fork of
[skeskinen/smartcut](https://github.com/skeskinen/smartcut). It makes
frame-accurate cuts while re-encoding only the GOPs around each cut point.

The frontend and backend ship together but remain separate processes:

- `mpv-smartcut.lua` owns selection, progress, cancellation, temporary files,
  atomic finalization, and cleanup.
- `mpv-smartcut-backend` is the minimal codec-aware worker invoked by the
  frontend.

The fork preserves the original MIT license and codec implementation. Its first
runtime change replaces NumPy timestamp indexing with Python standard-library
lists and binary search, avoiding a large numerical-computing closure.

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
quality=high
```

`quality` controls only the small re-encoded boundary GOPs and accepts `low`,
`normal`, `high`, `indistinguishable`, `near-lossless`, or `lossless`.
The default, `high`, targets near-lossless boundary quality. H.264, HEVC, VP8,
VP9, and AV1 use fast software encoder settings for these boundaries, trading
larger boundary GOPs for speed. HEVC and AV1 boundaries with 4:2:0 video use
NVENC when an NVIDIA GPU supports it, which is about 10 to 20 times faster than
software at equal quality.
Complete GOPs are copied unchanged. Encoding speed still depends on the codec,
resolution, and distance to the nearest keyframes.
Older MPEG encoders use quantizer-based quality. `lossless` requires a codec
that supports it, otherwise the highest available quality is used.

Cuts are written to a hidden partial file and renamed only after success.
Partial output is deleted when processing fails, is cancelled, or mpv exits.
