from fractions import Fraction
from typing import BinaryIO, Protocol

import av

from smartcut.media_container import MediaContainer
from smartcut.track_cutters import PassthroughCutter, create_output_stream
from smartcut.video_cutter import CutSegment, VideoCutter, create_video_output_stream


class Progress(Protocol):
    def emit(self, completed: int, total: int) -> None: ...


def adjust_range(
    selection: tuple[Fraction, Fraction],
    media_container: MediaContainer,
) -> tuple[Fraction, Fraction]:
    start, end = selection
    epsilon = Fraction(1, 1_000_000)
    if start <= epsilon:
        start = Fraction(-10)
    if media_container.duration is not None and end >= media_container.duration - epsilon:
        end = media_container.duration + 10
    return start + media_container.start_time, end + media_container.start_time


def smart_cut(
    media_container: MediaContainer,
    selection: tuple[Fraction, Fraction],
    output_path: str | BinaryIO,
    progress: Progress,
) -> None:
    start, end = adjust_range(selection, media_container)
    # A shared origin keeps the delays between streams.
    origin = max(start, media_container.start_time)
    total = max(1, int((selection[1] - selection[0]) * 1_000_000))
    with av.open(output_path, 'w') as output:
        output.metadata['ENCODED_BY'] = 'mpv-smartcut 0.1.0'
        container_name = (output.format.name or '').lower()
        if any(name in container_name for name in ('matroska', 'webm')):
            for stream in media_container.av_container.streams:
                if stream.type == 'attachment':
                    output.add_stream_from_template(stream)

        video_cutter = None
        if media_container.video_stream is not None:
            video_stream = create_video_output_stream(media_container, output)
            video_cutter = VideoCutter(media_container, video_stream, output)
        elif not any(stream.type == 'audio' for stream in media_container.passthrough_streams):
            raise ValueError('input has no audio or video streams')
        cutters = {
            stream.index: PassthroughCutter(stream, create_output_stream(output, stream), start, end, offset=origin)
            for stream in media_container.passthrough_streams
        }
        output.start_encoding()
        progress.emit(0, total)
        found_media = False

        for item in media_container.demux(end):
            if isinstance(item, av.Packet):
                timestamp = item.pts * item.time_base
                cut_packet = cutters[item.stream_index].packet(item)
                if cut_packet is None:
                    continue
                output.mux(cut_packet)
                found_media = True
                if video_cutter is not None:
                    continue
            else:
                assert video_cutter is not None
                gop, gop_end = item
                video_cutter.prepare_gop(gop)
                overlap_start, overlap_end = max(start, gop.start_time), min(end, gop_end)
                if overlap_start >= overlap_end:
                    continue
                found_media = True
                segment = CutSegment(
                    gop.start_time < start or gop_end > end,
                    overlap_start,
                    overlap_end,
                    gop.start_dts,
                    gop.end_dts,
                    gop.index,
                )
                video_cutter.segment_start_in_output = overlap_start - origin
                for packet in video_cutter.segment(segment):
                    output.mux(packet)
                timestamp = overlap_end
            completed = int((timestamp - selection[0] - media_container.start_time) * 1_000_000)
            progress.emit(max(0, min(total, completed)), total)
        if video_cutter is not None:
            for packet in video_cutter.finish():
                output.mux(packet)
        if not found_media:
            raise ValueError('selected range contains no media')
        progress.emit(total, total)
