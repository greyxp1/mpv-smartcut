from fractions import Fraction
from typing import Protocol

import av
from av.packet import Packet

from smartcut.media_container import MediaContainer
from smartcut.media_utils import VideoExportQuality
from smartcut.misc_data import CutSegment
from smartcut.track_cutters import (
    PassthruAudioCutter,
    SubtitleCutter,
    create_audio_output_stream,
    create_subtitle_output_stream,
)
from smartcut.video_cutter import VideoCutter, create_video_output_stream

class Progress(Protocol):
    def emit(self, completed: int, total: int) -> None: ...


class PacketGenerator(Protocol):
    def segment(self, cut_segment: CutSegment) -> list[Packet]: ...
    def finish(self) -> list[Packet]: ...


def adjust_range(
    selection: tuple[Fraction, Fraction],
    media_container: MediaContainer,
) -> tuple[Fraction, Fraction]:
    start, end = selection
    epsilon = Fraction(1, 1_000_000)
    if start <= epsilon:
        start = Fraction(-10)
    if end >= media_container.duration - epsilon:
        end = media_container.duration + 10
    return start + media_container.start_time, end + media_container.start_time


def make_cut_segments(
    media_container: MediaContainer,
    selection: tuple[Fraction, Fraction],
) -> list[CutSegment]:
    start, end = selection

    if media_container.video_stream is None:
        if not media_container.audio_tracks:
            raise ValueError("input has no audio or video streams")
        track = media_container.audio_tracks[0]
        start = max(start, track.frame_times[0])
        end = min(end, track.frame_times[-1] + Fraction(1, 10_000))
        return [CutSegment(False, start, end)]

    gop_starts = [
        *media_container.gop_start_times_pts_s,
        media_container.start_time + media_container.duration + Fraction(1, 10_000),
    ]
    segments = []
    for index, (gop_start, gop_end, start_dts, end_dts) in enumerate(
        zip(
            gop_starts[:-1],
            gop_starts[1:],
            media_container.gop_start_times_dts,
            media_container.gop_end_times_dts,
        )
    ):
        overlap_start = max(gop_start, start)
        overlap_end = min(gop_end, end)
        if overlap_start >= overlap_end:
            continue

        complete_gop = gop_start >= start and gop_end <= end
        segments.append(
            CutSegment(
                not complete_gop,
                overlap_start,
                overlap_end,
                start_dts,
                end_dts,
                index,
            )
        )
    return segments


def smart_cut(
    media_container: MediaContainer,
    selection: tuple[Fraction, Fraction],
    output_path: str,
    quality: VideoExportQuality,
    progress: Progress,
) -> None:
    cut_segments = make_cut_segments(
        media_container,
        adjust_range(selection, media_container),
    )
    if not cut_segments:
        raise ValueError("selected range contains no media")

    with av.open(output_path, "w") as output:
        output.metadata["ENCODED_BY"] = "mpv-smartcut 0.1.0"

        container_name = (output.format.name or "").lower()
        if any(name in container_name for name in ("matroska", "webm")):
            for stream in media_container.av_container.streams:
                if stream.type == "attachment":
                    output.add_stream_from_template(stream)

        generators: list[PacketGenerator] = []
        if media_container.video_stream is not None:
            stream_setup = create_video_output_stream(media_container, output)
            generators.append(VideoCutter(media_container, stream_setup, output, quality))

        for track_index in range(len(media_container.audio_tracks)):
            audio_stream = create_audio_output_stream(media_container, output, track_index)
            generators.append(PassthruAudioCutter(media_container, audio_stream, track_index))

        for track_index in range(len(media_container.subtitle_tracks)):
            subtitle_stream = create_subtitle_output_stream(media_container, output, track_index)
            generators.append(SubtitleCutter(media_container, subtitle_stream, track_index))

        output.start_encoding()
        total = len(cut_segments)
        progress.emit(0, total)
        for completed, segment in enumerate(cut_segments, 1):
            for generator in generators:
                for packet in generator.segment(segment):
                    if packet.dts is not None and packet.dts < -900_000:
                        packet.dts = None
                    output.mux(packet)
            progress.emit(completed, total)

        for generator in generators:
            for packet in generator.finish():
                output.mux(packet)
