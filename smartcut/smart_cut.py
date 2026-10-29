from collections.abc import Callable
from fractions import Fraction
from typing import BinaryIO

import av

from smartcut.media_container import MediaContainer
from smartcut.track_cutters import PassthroughCutter, create_output_stream
from smartcut.video_cutter import VideoCutter


def smart_cut(
    media_container: MediaContainer,
    selection: tuple[Fraction, Fraction],
    output_path: str | BinaryIO,
    progress: Callable[[int, int], None],
) -> None:
    # Selections touching either end of the file keep everything past it.
    epsilon, duration = Fraction(1, 1_000_000), media_container.duration
    start = (Fraction(-10) if selection[0] <= epsilon else selection[0]) + media_container.start_time
    end = (duration + 10 if duration is not None and selection[1] >= duration - epsilon else selection[1]) + media_container.start_time
    # A shared origin keeps the delays between streams.
    origin = max(start, media_container.start_time)
    total = max(1, int((selection[1] - selection[0]) * 1_000_000))
    with av.open(output_path, 'w') as output:
        output.metadata['ENCODED_BY'] = 'mpv-smartcut 0.1.0'
        if output.format.name in ('matroska', 'webm'):
            for stream in media_container.av_container.streams:
                if stream.type == 'attachment':
                    output.add_stream_from_template(stream)

        video_cutter = None
        if media_container.video_stream is not None:
            video_cutter = VideoCutter(media_container, output, origin)
        elif not any(stream.type == 'audio' for stream in media_container.passthrough_streams):
            raise ValueError('input has no audio or video streams')
        cutters = {
            stream.index: PassthroughCutter(stream, create_output_stream(output, stream), start, end, offset=origin)
            for stream in media_container.passthrough_streams
        }
        output.start_encoding()
        progress(0, total)
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
                gop, gop_end = item
                video_cutter.prepare_gop(gop)
                overlap_start, overlap_end = max(start, gop.start_time), min(end, gop_end)
                if overlap_start >= overlap_end:
                    continue
                found_media = True
                output.mux(video_cutter.segment(overlap_start, overlap_end, gop_end > end))
                timestamp = overlap_end
            completed = int((timestamp - selection[0] - media_container.start_time) * 1_000_000)
            progress(max(0, min(total, completed)), total)
        if video_cutter is not None:
            output.mux(video_cutter.finish())
        if not found_media:
            raise ValueError('selected range contains no media')
        progress(total, total)
