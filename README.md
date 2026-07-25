# mpv-smartcut

`mpv-smartcut` is a small mpv frontend and maintained packaging fork of
[skeskinen/smartcut](https://github.com/skeskinen/smartcut). It makes
frame-accurate cuts while re-encoding only the GOPs around each cut point.

The frontend and backend ship together but remain separate processes:

- `mpv-smartcut.lua` owns selection, progress, cancellation, temporary files,
  atomic finalization, and cleanup.
- `mpv-smartcut-backend` owns codec-aware smart rendering and remains usable as
  a standalone CLI.

The fork preserves the original MIT license and codec implementation. Its first
runtime change replaces NumPy timestamp indexing with Python standard-library
lists and binary search, avoiding a large numerical-computing closure.

## mpv usage

Copy `mpv-smartcut.lua` to mpv's `scripts` directory and ensure
`mpv-smartcut-backend` is in `PATH`. Press `c` at both ends of the desired
range. Press `C` to clear a selection or cancel a running cut.

Optional settings belong in `script-opts/mpv-smartcut.conf`; see
`mpv-smartcut.conf.example`.

Cuts are written to a hidden partial file and renamed only after success.
Partial output is deleted when processing fails, is cancelled, or mpv exits.

## Backend

The backend retains the original SmartCut CLI syntax:

```console
mpv-smartcut-backend input.mkv output.mkv --keep 10,20
```
