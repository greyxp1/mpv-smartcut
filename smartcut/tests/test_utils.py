import os
import platform
import subprocess
from collections.abc import Iterable
from fractions import Fraction
from itertools import pairwise
from typing import Any, cast

import ffmpeg
import numpy as np
import requests
import scipy.signal
import soundfile as sf
from av import AudioFrame, AudioStream, Packet, VideoFrame, VideoStream
from av import logging as av_logging
from av import open as av_open
from av import time_base as AV_TIME_BASE
from av.container.input import InputContainer
from av.stream import Disposition

from smartcut.media_container import AudioTrack, MediaContainer
from smartcut.media_utils import VideoExportMode, VideoExportQuality
from smartcut.misc_data import AudioExportInfo, AudioExportSettings
from smartcut.smart_cut import make_adjusted_segment_times, make_cut_segments, smart_cut
from smartcut.video_cutter import VideoSettings

AV_TIME_BASE_VALUE: int = int(AV_TIME_BASE) if AV_TIME_BASE else 1


def _ensure_fraction(value: Fraction | int | float) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(value).limit_denominator(1_000_000)
    return Fraction(value)


def to_fraction_segments(segments: Iterable[tuple[Fraction | int | float, Fraction | int | float]]) -> list[tuple[Fraction, Fraction]]:
    """Normalize segment boundaries to Fractions for type checking."""
    return [(_ensure_fraction(start), _ensure_fraction(end)) for start, end in segments]

def cached_download(url: str, name: str) -> str:
    if os.path.exists(name):
        return name

    response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})

    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")

    tmp_path = name + ".tmp"
    with open(tmp_path, "wb") as fh:
        chunk = response.content
        if chunk:
            fh.write(chunk)

    os.rename(tmp_path, name)

    return name


MEDIA_TEST_DATA_REPO = "https://github.com/skeskinen/media-test-data.git"
MEDIA_TEST_DATA_DIR = "media-test-data"


def get_media_test_data_file(filename: str) -> str:
    """Get a file from the media-test-data git repo, cloning if needed."""
    repo_dir = MEDIA_TEST_DATA_DIR
    file_path = os.path.join(repo_dir, filename)

    if os.path.exists(file_path):
        return file_path

    if not os.path.exists(repo_dir):
        # Clone the repo (shallow clone for speed)
        subprocess.run(
            ["git", "clone", "--depth", "1", MEDIA_TEST_DATA_REPO, repo_dir],
            check=True
        )
        # Pull LFS files if any (harmless if no LFS)
        subprocess.run(
            ["git", "-C", repo_dir, "lfs", "pull"],
            check=False  # Don't fail if git-lfs not installed
        )
    else:
        # Repo exists but file missing - try git pull
        subprocess.run(["git", "-C", repo_dir, "pull"], check=True)
        subprocess.run(["git", "-C", repo_dir, "lfs", "pull"], check=False)

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File {filename} not found in media-test-data repo")

    return file_path


def color_at_time(ts: float) -> np.ndarray:
    c = np.empty((3,))
    c[0] = 0.5 + 0.5 * np.sin(2 * np.pi * (0 / 3 + ts / 2.))
    c[1] = 0.5 + 0.5 * np.sin(2 * np.pi * (1 / 3 + ts / 2.))
    c[2] = 0.5 + 0.5 * np.sin(2 * np.pi * (2 / 3 + ts / 2.))

    c = np.round(255 * c).astype(np.uint8)
    c = np.clip(c, 0, 255)

    return c


def create_test_video(path: str, target_duration: int | float, codec: str, pixel_format: str, fps: int, resolution: tuple[int, int], x265_options: list[str] | None = None, profile: str | None = None) -> None:
    if os.path.exists(path):
        return
    total_frames = int(target_duration * fps)

    container = av_open(path, mode="w")

    if x265_options is None:
        x265_options = []
    x265_options = [*x265_options, 'log_level=warning']
    options = {'x265-params': ':'.join(x265_options)}
    if profile is not None:
        options['profile'] = profile
    stream = cast(VideoStream, container.add_stream(codec, rate=fps, options=options))
    stream.width = resolution[0]
    stream.height = resolution[1]
    stream.pix_fmt = pixel_format

    for frame_i in range(total_frames):

        img = np.empty((stream.width, stream.height, 3), dtype=np.uint8)
        c = color_at_time(frame_i / fps)
        img[:, :] = c

        frame = VideoFrame.from_ndarray(img, format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)

    container.close()

def av_write_ogg(path: str, wave: np.ndarray, sample_rate: int) -> None:
    with av_open(path, 'w') as out:
        stream = cast(AudioStream, out.add_stream('libvorbis', sample_rate, layout='mono'))
        wave = wave.astype(np.float32)
        wave = np.expand_dims(wave, 0)
        frame = AudioFrame.from_ndarray(wave, format='fltp', layout='mono')
        frame.sample_rate = sample_rate
        frame.pts = 0
        packets = []
        packets.extend(stream.encode(frame))
        packets.extend(stream.encode(None))
        for p in packets:
            out.mux(p)

def generate_sine_wave(duration: float, path: str, frequency: int = 440, sample_rate: int = 44100) -> None:
    if os.path.exists(path):
        return

    t = np.linspace(0, duration, int(duration * sample_rate), endpoint=False)
    wave = np.sin(2 * np.pi * frequency * t)

    if platform.system() == 'Windows' and os.path.splitext(path)[-1] == '.ogg':
        av_write_ogg(path, wave, sample_rate)
    else:
        sf.write(path, wave, sample_rate)

def generate_double_sine_wave(duration: float, path: str, frequency_0: int = 440, frequency_1: int = 440, sample_rate: int = 44100) -> None:
    if os.path.exists(path):
        return
    t = np.linspace(0, duration, int(duration * sample_rate), endpoint=False)
    wave_0 = np.sin(2 * np.pi * frequency_0 * t)
    wave_1 = np.sin(2 * np.pi * frequency_1 * t)
    wave = 0.5 * (wave_0 + wave_1)

    if platform.system() == 'Windows' and os.path.splitext(path)[-1] == '.ogg':
        av_write_ogg(path, wave, sample_rate)
    else:
        sf.write(path, wave, sample_rate)

def read_track_audio(track: AudioTrack) -> tuple[np.ndarray, int]:
    """Decode the entire audio stream for a test track into a numpy array."""
    decoded: list[np.ndarray] = []
    sample_rate: int | None = None
    expected_channels: int = 1
    fallback_rate: int = 48_000

    with av_open(track.path, 'r', metadata_errors='ignore') as container_raw:
        container = cast(InputContainer, container_raw)
        stream = cast(AudioStream, container.streams.audio[track.index])
        codec_context = stream.codec_context
        if codec_context is None:
            raise AssertionError("Audio stream is missing codec context")
        codec_context.thread_type = "FRAME"

        expected_channels = stream.channels or expected_channels
        fallback_rate = stream.rate or fallback_rate

        for packet in container.demux(stream):
            if not isinstance(packet, Packet):
                continue
            for frame in packet.decode():
                if not isinstance(frame, AudioFrame):
                    continue
                array = frame.to_ndarray()

                if array.ndim == 1:
                    array = np.expand_dims(array, 0)

                if array.ndim != 2:
                    raise AssertionError(f"Unexpected audio frame shape {array.shape}")

                if array.shape[0] != expected_channels:
                    if array.shape[1] == expected_channels:
                        array = array.T
                    else:
                        raise AssertionError(f"Unexpected audio frame shape {array.shape} for {expected_channels} channel(s)")

                if np.issubdtype(array.dtype, np.integer):
                    dtype_obj = cast(np.dtype[np.integer[Any]], array.dtype)
                    info = np.iinfo(dtype_obj)
                    scale = max(abs(info.min), abs(info.max))
                    if scale == 0:
                        raise AssertionError("Audio frame has zero dynamic range")
                    array = array.astype(np.float32) / scale
                elif np.issubdtype(array.dtype, np.floating):
                    array = array.astype(np.float32, copy=False)
                else:
                    raise AssertionError(f"Unsupported audio dtype {array.dtype}")

                decoded.append(array)
                if frame.sample_rate is not None:
                    sample_rate = frame.sample_rate

    if not decoded:
        return np.zeros((expected_channels, 0), dtype=np.float32), sample_rate or fallback_rate

    audio = np.concatenate(decoded, axis=1)
    return audio, sample_rate or fallback_rate

def compare_tracks(track_orig: AudioTrack, track_modified: AudioTrack, rms_threshold: float = 0.07) -> None:
    y_orig, _ = read_track_audio(track_orig)
    y_modified, _ = read_track_audio(track_modified)

    expected_samples = y_orig.shape[1]
    if expected_samples == 0:
        raise AssertionError("Audio track has no samples to compare")

    assert y_orig.shape == y_modified.shape, f"Audio shape was modified. {y_orig.shape} -> {y_modified.shape}"

    ALLOW_PADDING = False
    if ALLOW_PADDING:
        if y_modified.shape[1] > expected_samples:
            y_modified = y_modified[:, :expected_samples]
        elif y_modified.shape[1] < expected_samples:
            pad_width = expected_samples - y_modified.shape[1]
            y_modified = np.pad(y_modified, ((0, 0), (0, pad_width)))

    corr_ref = scipy.signal.correlate(y_orig, y_orig)
    corr_inp = scipy.signal.correlate(y_orig, y_modified)

    corr_ref = np.mean(np.abs(corr_ref))
    corr_inp = np.mean(np.abs(corr_inp))

    corr_diff = (corr_ref - corr_inp) / corr_ref
    assert corr_diff < 0.1, f"Audio contents have changed: {corr_diff} (correlation difference)"

    # TODO: come up with better audio similarity metric?

    diff = (y_orig - y_modified) ** 2
    rms = np.sqrt(np.mean(diff))

    assert rms < rms_threshold, f"Audio contents have changed: {rms} (rms error)"

def check_audio_pts_timestamps(container: MediaContainer, max_expected_duration: float, test_name: str = "") -> None:
    """
    Validate that audio PTS timestamps start near 0 and end near the expected duration.

    This checks that audio-only files don't have incorrectly offset PTS timestamps
    (e.g., offset by 10 seconds due to video pre-roll extension being applied to audio-only files).

    Args:
        container: MediaContainer with audio track to check
        max_expected_duration: Maximum expected PTS time for the last packet (in seconds)
        test_name: Optional prefix for error messages
    """
    assert len(container.audio_tracks) > 0, "Container has no audio tracks"

    track = container.audio_tracks[0]
    time_base = track.av_stream.time_base
    assert time_base is not None, "Time base should not be None"

    assert len(track.packets) > 0, "Audio track has no packets"

    first_packet = track.packets[0]
    last_packet = track.packets[-1]

    prefix = f"{test_name}: " if test_name else ""

    # Check first packet PTS is near 0
    assert first_packet.pts is not None, f"{prefix}First packet should have PTS"
    first_pts_time = float(first_packet.pts * time_base)
    assert first_pts_time < 0.5, \
        f"{prefix}First packet PTS should be near 0, got {first_pts_time:.6f}s (PTS={first_packet.pts})"

    # Check last packet PTS is within expected range
    assert last_packet.pts is not None, f"{prefix}Last packet should have PTS"
    last_pts_time = float(last_packet.pts * time_base)
    assert last_pts_time < max_expected_duration + 0.5, \
        f"{prefix}Last packet PTS should be near {max_expected_duration}s, got {last_pts_time:.6f}s (PTS={last_packet.pts})"

def _median_fraction(values: list[Fraction]) -> Fraction | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _infer_frame_count_anomaly(source_times: list[Fraction], result_times: list[Fraction]) -> dict[str, Any] | None:
    """Find the largest output gap and provide nearby PTS context.

    Returns dict with:
      - kind: 'missing'|'extra' based on frame count delta
      - result_gap_index: index in result where the gap occurs (between j and j+1)
      - gap: size of the gap (Fraction)
      - expected: median expected frame interval from source (Fraction)
      - result_pts_around: list of (idx, float_secs) for up to 5 frames around the gap
      - source_pts_near_gap: list of (idx, float_secs) near the gap time in the source
    Returns None if nothing sensible can be inferred.
    """
    try:
        n_s = len(source_times)
        n_r = len(result_times)
        if n_s < 2 or n_r < 2:
            return None

        # Expected frame interval from source timings (robust median)
        s_diffs = [source_times[i + 1] - source_times[i] for i in range(n_s - 1)]
        dt = _median_fraction(s_diffs)
        if dt is None or dt == 0:
            return None

        # Find the largest gap in result
        r_diffs = [result_times[i + 1] - result_times[i] for i in range(n_r - 1)]
        if not r_diffs:
            return None
        j = int(np.argmax(r_diffs))  # type: ignore[arg-type]
        gap = r_diffs[j]

        # Prepare PTS context around the gap in result
        def _pts_slice(arr: list[Fraction], center_left_idx: int, window: int = 2) -> list[tuple[int, float | Fraction]]:
            start = max(0, center_left_idx - window)
            end = min(len(arr) - 1, center_left_idx + window + 1)  # inclusive end index for frames
            vals = []
            for idx in range(start, end + 1):
                try:
                    vals.append((idx, float(arr[idx])))
                except Exception:
                    vals.append((idx, arr[idx]))
            return vals

        result_pts_around = _pts_slice(result_times, j, window=2)

        # Choose a representative time inside the gap and show nearby source PTS
        mid_time = result_times[j] + gap / 2
        k = int(np.argmin([abs(st - mid_time) for st in source_times]))  # type: ignore[arg-type]
        # Build symmetric window around k
        src_start = max(0, k - 2)
        src_end = min(len(source_times) - 1, k + 2)
        source_pts_near_gap = []
        for idx in range(src_start, src_end + 1):
            try:
                source_pts_near_gap.append((idx, float(source_times[idx])))
            except Exception:
                source_pts_near_gap.append((idx, source_times[idx]))

        return {
            'kind': 'missing' if n_r < n_s else 'extra' if n_r > n_s else 'unknown',
            'result_gap_index': j + 1,  # gap is between j and j+1
            'gap': gap,
            'expected': dt,
            'result_pts_around': result_pts_around,
            'source_pts_near_gap': source_pts_near_gap,
        }
    except Exception:
        return None


def check_videos_equal(source_container: MediaContainer, result_container: MediaContainer, pixel_tolerance: int = 20, allow_failed_frames: int = 0, allow_failed_pixels_per_frame: int = 0, timing_tolerance: Fraction | None = None) -> None:
    source_stream = source_container.video_stream
    result_stream = result_container.video_stream
    assert source_stream is not None, "Source container is missing a video stream"
    assert result_stream is not None, "Result container is missing a video stream"
    assert source_stream.width == result_stream.width
    assert source_stream.height == result_stream.height
    if len(result_container.video_frame_times) != len(source_container.video_frame_times):
        # Provide a concise, output-focused gap report with local PTS context
        s = source_container.video_frame_times
        r = result_container.video_frame_times
        hint = _infer_frame_count_anomaly(list(s), list(r))
        if hint is not None:
            def _fmt_pts(series: list[tuple[int, float | Fraction]]) -> str:
                return ", ".join([f"{i}:{t:.6f}s" if isinstance(t, float) else f"{i}:{t}" for i, t in series])

            gap_note = ''
            if hint.get('gap') is not None and hint.get('expected') is not None:
                try:
                    gap_note = f" (gap {float(hint['gap']):.6f}s vs expected {float(hint['expected']):.6f}s)"
                except Exception:
                    gap_note = f" (gap {hint['gap']} vs expected {hint['expected']})"

            msg = (
                f"Mismatch frame count. Exp: {len(s)}, got: {len(r)}. "
                f"Largest gap in output around result index {hint['result_gap_index']}{gap_note}.\n"
                f"Result PTS around gap: [{_fmt_pts(hint['result_pts_around'])}]\n"
                f"Source PTS near gap:  [{_fmt_pts(hint['source_pts_near_gap'])}]\n"
                f"Last packet PTS (source -> result): {float(s[-1])} -> {float(r[-1])}"
            )
        else:
            msg = f"Mismatch frame count. Exp: {len(s)}, got: {len(r)}"
        assert len(result_container.video_frame_times) == len(source_container.video_frame_times), msg
    r = np.asarray(result_container.video_frame_times)
    s = np.asarray(source_container.video_frame_times)

    diff = np.abs(r - s)
    diff_i = np.argmax(diff)
    diff_amount = diff[diff_i]
    # NOTE: It would be nice to get a tighter bound on the frame timings.
    # But the difficulty is that we can't control the output stream time_base.
    # We have to just accept the value that av/ffmpeg gives us. So sometimes the
    # input and output timebases are not multiples of each other.
    is_mpeg = source_container.av_container.format.name in ['mpegts', 'mpegvideo']
    is_avi = source_container.av_container.format.name == 'avi'
    if timing_tolerance is not None:
        diff_tolerance = timing_tolerance
    elif is_mpeg:
        diff_tolerance = Fraction(2)
    elif is_avi:
        diff_tolerance = Fraction(1, 10)
    else:
        diff_tolerance = Fraction(3, 1000)

    assert diff_amount <= diff_tolerance, f'Mismatch of {diff_amount} in frame timings, at frame {diff_i}.'

    failed_frame_info: list[tuple[int, float]] = []  # (frame_index, time_seconds)
    frame_times = source_container.video_frame_times

    # First, decode source alone to capture baseline warnings (some source files have inherent issues)
    # Also trigger swscaler by converting first frame to RGB24, since the comparison phase does this
    av_logging.set_level(av_logging.WARNING)
    try:
        with av_logging.Capture() as source_logs, av_open(source_container.path, mode='r') as source_av_raw:
            source_av = cast(InputContainer, source_av_raw)
            triggered_swscaler = False
            for frame in source_av.decode(video=0):
                if not triggered_swscaler and isinstance(frame, VideoFrame):
                    frame.to_ndarray(format='rgb24')  # Trigger swscaler warnings once
                    triggered_swscaler = True
    finally:
        av_logging.set_level(av_logging.FATAL)
    source_warnings = {msg for level, _, msg in source_logs if level <= av_logging.WARNING}

    # Capture decode warnings while comparing pixels
    av_logging.set_level(av_logging.WARNING)
    try:
        with av_logging.Capture() as logs, av_open(source_container.path, mode='r') as source_av_raw, av_open(result_container.path, mode='r') as result_av_raw:
            source_av = cast(InputContainer, source_av_raw)
            result_av = cast(InputContainer, result_av_raw)
            for frame_i, (source_frame, result_frame) in enumerate(zip(source_av.decode(video=0), result_av.decode(video=0))):
                if not isinstance(source_frame, VideoFrame) or not isinstance(result_frame, VideoFrame):
                    continue
                source_numpy = source_frame.to_ndarray(format='rgb24')
                result_numpy = result_frame.to_ndarray(format='rgb24')
                assert source_numpy.shape == result_numpy.shape, f'Video resolution or channel count changed. Exp: {source_numpy.shape}, got: {result_numpy.shape}'

                failed_pixels_in_frame = 0
                last_fail_colors = None
                for y in [0, source_numpy.shape[0] // 2, source_numpy.shape[0] - 1]:
                    for x in [0, source_numpy.shape[1] // 2, source_numpy.shape[1] - 1]:
                        source_color = source_numpy[y, x]
                        result_color = result_numpy[y, x]
                        diff = np.abs(source_color.astype(np.int16) - result_color)
                        max_diff = np.max(diff)
                        if max_diff > pixel_tolerance:
                            failed_pixels_in_frame += 1
                            last_fail_colors = (source_color, result_color)

                if failed_pixels_in_frame > allow_failed_pixels_per_frame:
                    frame_time = float(frame_times[frame_i]) if frame_i < len(frame_times) else 0.0
                    if len(failed_frame_info) >= allow_failed_frames:
                        failed_frame_info.append((frame_i, frame_time))
                        prev_failures = ", ".join(f"#{f}@{t:.3f}s" for f, t in failed_frame_info[:-1])
                        source_color, result_color = last_fail_colors if last_fail_colors else (None, None)
                        raise AssertionError(
                            f'Large color deviation at frame {frame_i} ({frame_time:.3f}s), {failed_pixels_in_frame}/9 pixels failed. '
                            f'Exp: {source_color}, got: {result_color}. '
                            f'Failed frames ({len(failed_frame_info)}/{allow_failed_frames + 1}): {prev_failures}, #{frame_i}@{frame_time:.3f}s. '
                            f'{result_container.path}'
                        )
                    failed_frame_info.append((frame_i, frame_time))
    finally:
        av_logging.set_level(av_logging.FATAL)

    # Check for decode warnings (excluding warnings that were already present in source)
    error_msgs = [msg for level, _, msg in logs if level <= av_logging.WARNING and msg not in source_warnings]
    assert len(error_msgs) == 0, (
        f"Video has {len(error_msgs)} decode warnings:\n" +
        "\n".join(error_msgs[:10])
    )

def check_videos_equal_segment(source_container: MediaContainer, result_container: MediaContainer, start_time: float | Fraction = 0.0, duration: float | None = None, pixel_tolerance: int = 20) -> None:
    """Fast pixel testing of small video segments instead of entire video"""
    if duration is None:
        duration = min(10, float(source_container.duration))  # Test max 10 seconds

    start = float(start_time)
    end_time = start + duration

    # Basic structure checks
    source_stream = source_container.video_stream
    result_stream = result_container.video_stream
    assert source_stream is not None, "Source container is missing a video stream"
    assert result_stream is not None, "Result container is missing a video stream"
    assert source_stream.width == result_stream.width
    assert source_stream.height == result_stream.height

    frames_checked = 0
    seek_target = int(start * AV_TIME_BASE_VALUE)
    with av_open(source_container.path, mode='r') as source_av_raw, av_open(result_container.path, mode='r') as result_av_raw:
        source_av = cast(InputContainer, source_av_raw)
        result_av = cast(InputContainer, result_av_raw)
        # Seek to start time
        source_av.seek(seek_target)
        result_av.seek(seek_target)

        source_decoder = source_av.decode(video=0)
        result_decoder = result_av.decode(video=0)

        for source_frame, result_frame in zip(source_decoder, result_decoder):
            if not isinstance(source_frame, VideoFrame) or not isinstance(result_frame, VideoFrame):
                continue
            if source_frame.pts is None or source_frame.time_base is None:
                continue
            frame_time = float(source_frame.pts * source_frame.time_base)
            if frame_time < start:
                continue
            if frame_time > end_time:
                break

            source_numpy = source_frame.to_ndarray(format='rgb24')
            result_numpy = result_frame.to_ndarray(format='rgb24')
            assert source_numpy.shape == result_numpy.shape, f'Video resolution changed at {frame_time:.2f}s. Exp: {source_numpy.shape}, got: {result_numpy.shape}'

            # Check a few pixels per frame for speed
            for y in [0, source_numpy.shape[0] // 2, source_numpy.shape[0] - 1]:
                for x in [0, source_numpy.shape[1] // 2, source_numpy.shape[1] - 1]:
                    source_color = source_numpy[y, x]
                    result_color = result_numpy[y, x]
                    diff = np.abs(source_color.astype(np.int16) - result_color)
                    max_diff = np.max(diff)
                    assert max_diff <= pixel_tolerance, f'Large color deviation at {frame_time:.2f}s. Exp: {source_color}, got: {result_color}'

            frames_checked += 1
            if frames_checked >= 100:  # Limit frames for speed
                break

def run_cut_on_keyframes_test(input_path: str, output_path: str) -> None:
    source = MediaContainer(input_path)
    cutpoints = [*source.gop_start_times_pts_s, source.duration]

    segments = list(pairwise(cutpoints))
    segments = to_fraction_segments(segments)

    segments = make_adjusted_segment_times(segments, source)

    cut_segments = make_cut_segments(source, segments)
    for c in cut_segments:
        assert not c.require_recode, "Cutting on a keyframe should not require recoding"

    smart_cut(source, segments, output_path)

    result_container = MediaContainer(output_path)
    check_videos_equal(source, result_container)

def run_smartcut_test(input_path: str, output_path: str, n_cuts: int, audio_export_info: AudioExportInfo | str | None = None, video_settings: VideoSettings | None = None, pixel_tolerance: int = 20, allow_failed_frames: int = 0) -> None:
    if os.path.splitext(input_path)[1] in ['.mp3', '.ogg']:
        return run_audiofile_smartcut(input_path, output_path, n_cuts)
    source = MediaContainer(input_path)
    cutpoints = source.video_frame_times
    cutpoints = [0, *np.sort(np.random.choice(cutpoints, n_cuts, replace=False)), source.duration + 1]

    segments = list(pairwise(cutpoints))
    segments = to_fraction_segments(segments)

    if audio_export_info == 'auto':
        s = AudioExportSettings(codec='passthru')
        audio_export_info = AudioExportInfo(output_tracks=[s] * len(source.audio_tracks))

    # Type narrowing: after the check above, audio_export_info is AudioExportInfo | None
    assert audio_export_info is None or isinstance(audio_export_info, AudioExportInfo)
    smart_cut(source, segments, output_path,
        audio_export_info=audio_export_info, video_settings=video_settings, log_level='warning')

    result_container = MediaContainer(output_path)
    check_videos_equal(source, result_container, pixel_tolerance=pixel_tolerance, allow_failed_frames=allow_failed_frames)

def run_audiofile_smartcut(input_path: str, output_path: str, n_cuts: int) -> None:
    source_container = MediaContainer(input_path)
    duration = source_container.duration
    cutpoints = np.arange(duration*1000)[1:-1]
    cutpoints = [0] + [Fraction(x, 1000) for x in np.sort(np.random.choice(cutpoints, n_cuts, replace=False))] + [duration]

    segments = list(pairwise(cutpoints))
    segments = to_fraction_segments(segments)

    settings = AudioExportSettings(codec='passthru')
    export_info = AudioExportInfo(output_tracks=[settings])

    smart_cut(source_container, segments, output_path, audio_export_info=export_info)
    output_container = MediaContainer(output_path)

    compare_tracks(source_container.audio_tracks[0], output_container.audio_tracks[0])


def run_partial_smart_cut(
    input_path: str,
    output_base_name: str,
    segment_duration: int = 15,
    n_segments: int = 2,
    audio_export_info: AudioExportInfo | str | None = None,
    video_settings: VideoSettings | None = None,
    pixel_tolerance: int = 20,
    allow_failed_frames: int = 0,
    recode_codec_override: str | None = None,
    allow_failed_pixels_per_frame: int = 0,
) -> None:
    """
    Test smart cutting on short segments from random positions in long videos.

    Selects multiple random segments and merges them into a single output file,
    then compares smart cut output against complete recode for quality validation.
    This tests both the cutting algorithm and segment merging logic while being
    much faster than testing the entire video.

    Args:
        input_path: Path to input video file
        output_base_name: Base name for output files (will be suffixed)
        segment_duration: Duration of each test segment in seconds
        n_segments: Number of random segments to merge into one file
        audio_export_info: Audio export settings (defaults to passthrough)
        video_settings: Video export settings for smart cut
        pixel_tolerance: Pixel difference tolerance for comparison
    """
    # Handle audio-only files
    if os.path.splitext(input_path)[1] in ['.mp3', '.ogg']:
        return run_audiofile_smartcut(input_path, output_base_name + '.ogg', 2)

    source = MediaContainer(input_path)
    total_duration = float(source.duration)

    # Skip if video is too short for meaningful testing
    assert total_duration >= segment_duration * n_segments * 2, f"Video too short ({total_duration:.1f}s) for partial testing, use regular test"

    # Calculate safe range for random segment selection (avoid first/last 30 seconds)
    safe_start = 30
    safe_end = total_duration - segment_duration - 30

    if safe_end <= safe_start:
        safe_start = segment_duration
        safe_end = total_duration - segment_duration

    # print(f"Testing {n_segments} segments of {segment_duration}s each merged into one file from {input_path}")

    # Set up default audio export if not specified
    if audio_export_info == 'auto':
        s = AudioExportSettings(codec='passthru')
        audio_export_info = AudioExportInfo(output_tracks=[s] * len(source.audio_tracks))

    # Type narrowing: after the check above, audio_export_info is AudioExportInfo | None
    assert audio_export_info is None or isinstance(audio_export_info, AudioExportInfo)

    # Select multiple random non-overlapping segments
    segments = []

    # Generate all possible non-overlapping segments
    max_segments = int((safe_end - safe_start) // (segment_duration + 1))  # +1 for minimum gap
    if max_segments < n_segments:
        print(f"  Warning: Can only fit {max_segments} non-overlapping segments, reducing from {n_segments}")
        n_segments = max_segments

    # Use a more systematic approach to ensure non-overlapping segments
    attempts = 0
    while len(segments) < n_segments and attempts < 100:
        start_time = safe_start + np.random.random() * (safe_end - safe_start)
        end_time = start_time + segment_duration

        # Check if this segment overlaps with any existing segment
        overlaps = False
        for existing_start, existing_end in segments:
            # Two segments overlap if: start < existing_end AND end > existing_start
            if start_time < existing_end and end_time > existing_start:
                overlaps = True
                break

        if not overlaps and end_time <= safe_end:
            segments.append((start_time, end_time))

        attempts += 1

    if len(segments) < n_segments:
        print(f"  Warning: Could only find {len(segments)} non-overlapping segments out of {n_segments} requested")

    # Sort segments by start time (smart_cut expects them in chronological order)
    segments.sort(key=lambda x: x[0])

    # Print selected segments in chronological order
    # for i, (start, end) in enumerate(segments):
    #     print(f"  Segment {i+1}: {start:.1f}s to {end:.1f}s")

    # Verify segments are non-overlapping after sorting
    for i in range(len(segments) - 1):
        current_end = segments[i][1]
        next_start = segments[i + 1][0]
        if current_end > next_start:
            raise ValueError(f"Segments overlap after sorting: segment {i+1} ends at {current_end:.1f}s but segment {i+2} starts at {next_start:.1f}s")

    # Output paths
    smartcut_output = output_base_name + "_smartcut" + os.path.splitext(input_path)[1]
    recode_output = output_base_name + "_recode" + os.path.splitext(input_path)[1]

    fraction_segments = to_fraction_segments(segments)

    # Test 1: Smart cut (default mode) - merges all segments into one file
    smart_cut_settings = video_settings or VideoSettings(VideoExportMode.SMARTCUT, VideoExportQuality.HIGH)
    smart_cut(source, fraction_segments, smartcut_output,
             audio_export_info=audio_export_info,
             video_settings=smart_cut_settings,
             log_level='warning')

    # Test 2: Complete recode for comparison - merges all segments into one file
    recode_settings = (VideoSettings(VideoExportMode.RECODE, VideoExportQuality.HIGH, codec_override=recode_codec_override)
                       if recode_codec_override is not None else VideoSettings(VideoExportMode.RECODE, VideoExportQuality.HIGH))
    smart_cut(source, fraction_segments, recode_output,
             audio_export_info=audio_export_info,
             video_settings=recode_settings,
             log_level='warning')

    # Compare results - both should produce equivalent output
    smartcut_container = MediaContainer(smartcut_output)
    recode_container = MediaContainer(recode_output)

    check_videos_equal(smartcut_container, recode_container, pixel_tolerance=pixel_tolerance, allow_failed_frames=allow_failed_frames, allow_failed_pixels_per_frame=allow_failed_pixels_per_frame)

    smartcut_container.close()
    recode_container.close()

    source.close()

def check_stream_dispositions(source_path: str, output_path: str) -> None:
    """Helper function to verify that stream dispositions are preserved"""
    with av_open(source_path) as source_raw, av_open(output_path) as output_raw:
        source_container = cast(InputContainer, source_raw)
        output_container = cast(InputContainer, output_raw)
        # Check video stream disposition if it exists
        if source_container.streams.video:
            src_video = source_container.streams.video[0]
            out_video = output_container.streams.video[0]
            assert src_video.disposition.value == out_video.disposition.value, \
                    f"Video disposition mismatch: {src_video.disposition} vs {out_video.disposition}"

        # Check audio stream dispositions
        assert len(source_container.streams.audio) == len(output_container.streams.audio), \
                "Audio stream count mismatch"
        for i, (src_audio, out_audio) in enumerate(zip(source_container.streams.audio, output_container.streams.audio)):
            assert src_audio.disposition.value == out_audio.disposition.value, \
                    f"Audio stream {i} disposition mismatch: {src_audio.disposition} vs {out_audio.disposition}"

        # Check subtitle stream dispositions - this is the main focus
        assert len(source_container.streams.subtitles) == len(output_container.streams.subtitles), \
                "Subtitle stream count mismatch"
        for i, (src_sub, out_sub) in enumerate(zip(source_container.streams.subtitles, output_container.streams.subtitles)):
            assert src_sub.disposition.value == out_sub.disposition.value, \
                    f"Subtitle stream {i} disposition mismatch: {src_sub.disposition} vs {out_sub.disposition}"

            # Specifically check for forced flag preservation
            src_forced = Disposition.forced in src_sub.disposition
            out_forced = Disposition.forced in out_sub.disposition
            assert src_forced == out_forced, \
                    f"Subtitle stream {i} forced flag mismatch: source={src_forced}, output={out_forced}"


def make_video_and_audio_mkv(path: str, file_duration: float) -> None:

    audio_file_440 = 'tmp_440.ogg'
    # audio_file_440 = 'tmp_440.aac'
    generate_sine_wave(file_duration, audio_file_440, frequency=440)

    audio_file_630 = 'tmp_630.ogg'
    # audio_file_630 = 'tmp_630.aac'
    generate_sine_wave(file_duration, audio_file_630, frequency=630)

    tmp_video = 'tmp_video.mkv'
    create_test_video(tmp_video, file_duration, 'h264', 'yuv420p', 30, (32, 18))

    (
        ffmpeg
        .input(tmp_video)
        .output(ffmpeg.input(audio_file_440), ffmpeg.input(audio_file_630),
                path, vcodec='copy', acodec='aac', audio_bitrate=92_000, y=None)
        .run(quiet=True)
    )

def make_video_with_subtitles(path: str, file_duration: float, subtitle_configs: list[dict[str, str]]) -> str:
    """
    Create a video with multiple subtitle tracks.

    Args:
        path: Output file path
        file_duration: Duration in seconds
        subtitle_configs: List of dicts with keys:
            - 'content': SRT content string
            - 'language': Language code (e.g., 'en', 'fi')
            - 'disposition': Disposition string (e.g., 'forced+default', 'default', '')
            - 'temp_file': Temporary SRT file path
    """
    if os.path.exists(path):
        return path

    # Create base video
    tmp_video = 'tmp_video_for_sub.mkv'
    create_test_video(tmp_video, file_duration, 'h264', 'yuv420p', 30, (32, 18))

    # Create subtitle files
    subtitle_inputs = []
    for config in subtitle_configs:
        with open(config['temp_file'], 'w', encoding='utf-8') as f:
            f.write(config['content'])
        subtitle_inputs.append(ffmpeg.input(config['temp_file']))

    # Build ffmpeg command
    video_input = ffmpeg.input(tmp_video)

    # Prepare output options
    output_options = {
        'vcodec': 'copy',
        'scodec': 'subrip',
        'y': None
    }

    # Add disposition and language options for each subtitle stream
    for i, config in enumerate(subtitle_configs):
        if config.get('disposition'):
            output_options[f'disposition:s:{i}'] = config['disposition']
        if config.get('language'):
            output_options[f'metadata:s:s:{i}'] = f'language={config["language"]}'

    # Run ffmpeg command with proper input structure
    all_inputs = [video_input, *subtitle_inputs]
    (
        ffmpeg
        .output(*all_inputs, path, **output_options)
        .run(quiet=True)
    )

    return path

def make_video_with_attachment(path: str, file_duration: int = 3,
                               attachment_filename: str = 'smartcut_attachment.txt',
                               attachment_payload: bytes = b'SmartCutAttachmentTest') -> str:
    """Create a small MKV file that carries a single attachment stream."""
    if os.path.exists(path):
        return path

    base_video = 'tmp_attachment_base.mkv'
    create_test_video(base_video, file_duration, 'h264', 'yuv420p', 25, (32, 18))

    attachment_path = 'tmp_attachment_payload.bin'
    with open(attachment_path, 'wb') as fh:
        fh.write(attachment_payload)

    output_options = {
        'c': 'copy',
        'attach': attachment_path,
        'metadata:s:t': f'filename={attachment_filename}',
        'metadata:s:t:0': 'mimetype=text/plain',
        'y': None,
    }

    (
        ffmpeg
        .output(ffmpeg.input(base_video), path, **output_options)
        .run(quiet=True)
    )

    return path

def get_attachment_stream_metadata(path: str) -> list[dict[str, Any]]:
    """Extract attachment metadata and raw bytes using PyAV.

    Returns a list of dicts with keys:
      - 'filename': attachment filename (if present)
      - 'mimetype': attachment mimetype (if present)
      - 'extradata_size': size of raw data
      - 'data': raw attachment bytes
    """
    attachments = []
    with av_open(path) as container:
        # Use the dedicated attachments accessor; matches PyAV's own tests
        for att in container.streams.attachments:
            # For MKV, PyAV exposes name/mimetype/data directly
            name = getattr(att, 'name', None)
            mimetype = getattr(att, 'mimetype', None)
            data_bytes = getattr(att, 'data', None)

            # Also pick filename/mimetype from metadata if present
            md = dict(getattr(att, 'metadata', {}) or {})
            filename = name or md.get('filename')
            mimetype = mimetype or md.get('mimetype')

            attachments.append({
                'filename': filename,
                'mimetype': mimetype,
                'extradata_size': (len(data_bytes) if data_bytes is not None else None),
                'data': data_bytes,
            })

    attachments.sort(key=lambda info: info.get('filename') or '')
    return attachments

def make_video_with_forced_subtitle(path: str, file_duration: float) -> str:
    """Legacy function - creates video with single forced subtitle for backward compatibility"""
    subtitle_content = """1
00:00:01,000 --> 00:00:03,000
First subtitle entry

2
00:00:05,000 --> 00:00:07,000
Second forced subtitle

3
00:00:08,500 --> 00:00:09,500
Final entry
"""

    subtitle_configs = [{
        'content': subtitle_content,
        'language': 'en',
        'disposition': 'forced+default',
        'temp_file': 'tmp_test_subtitle_forced.srt'
    }]

    return make_video_with_subtitles(path, file_duration, subtitle_configs)

def get_tears_of_steel_annexb() -> str:
    """
    Get Tears of Steel in Annex B format (TS container) for testing H.264 NAL parsing.
    Converts from MP4 to TS to force Annex B NAL format.
    """
    mp4_filename = cached_download('http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/TearsOfSteel.mp4', 'google_tears_of_steel.mp4')
    ts_filename = 'google_tears_of_steel_annexb.ts'

    if not os.path.exists(ts_filename):
        # Convert first ~200 seconds to TS format (covers our problematic non-IDR keyframes)
        result = subprocess.run([
            'ffmpeg', '-i', mp4_filename,
            '-t', '200',  # First 200 seconds (covers 18.5s, 143.4s, 183.3s non-IDR frames)
            '-c', 'copy',  # Stream copy to preserve exact H.264 stream
            '-f', 'mpegts',  # Force MPEG-TS output (uses Annex B)
            '-y', ts_filename
        ], check=False, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to convert to TS format: {result.stderr}")

    return ts_filename


def check_no_discard_packets(output_path: str) -> None:
    """Verify that no video packets in the output have the discard flag set.

    The discard flag (AV_PKT_FLAG_DISCARD) tells decoders to skip the packet.
    This should never be set on video packets in smartcut output - we either
    copy packets directly or encode new ones, neither of which should produce
    discard-flagged packets.

    Raises:
        AssertionError: If any video packet has is_discard=True
    """
    with av_open(output_path, mode='r') as container:
        video_streams = [s for s in container.streams if s.type == 'video']
        if not video_streams:
            return  # No video to check

        discard_packets: list[tuple[int, float | None]] = []
        for packet in container.demux(video_streams[0]):
            if packet.is_discard:
                pts_time = float(packet.pts * packet.time_base) if packet.pts is not None and packet.time_base else None
                discard_packets.append((packet.pts or -1, pts_time))

        assert len(discard_packets) == 0, (
            f"Found {len(discard_packets)} video packet(s) with discard flag set. "
            f"First few: {discard_packets[:5]}"
        )


def get_testvideos_jellyfish_h265_ts() -> str:
    """Fetch Jellyfish HEVC sample and convert it to Annex B in an MPEG-TS container."""
    mp4_filename = cached_download(
        'https://test-videos.co.uk/vids/jellyfish/mp4/h265/360/Jellyfish_360_10s_1MB.mp4',
        'testvideos_jellyfish_h265.mp4'
    )
    ts_filename = 'testvideos_jellyfish_h265.ts'

    if not os.path.exists(ts_filename):
        result = subprocess.run([
            'ffmpeg', '-i', mp4_filename,
            '-c', 'copy',
            '-f', 'mpegts',
            '-y', ts_filename
        ], check=False, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to convert HEVC sample to TS format: {result.stderr}")

    return ts_filename
