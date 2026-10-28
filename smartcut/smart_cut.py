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
        streams = {
            stream.index: create_output_stream(output, stream)
            for stream in media_container.passthrough_streams
        }
        output.start_encoding()
        progress.emit(0, total)
        cutters = {}
        found_media = False

        if video_cutter is not None:
            for gop, gop_end in media_container.gops(end):
                video_cutter.prepare_gop(gop)
                overlap_start, overlap_end = max(start, gop.start_time), min(end, gop_end)
                if overlap_start >= overlap_end:
                    # Discard audio/subtitles from decoder priming before the selection.
                    list(media_container.take_passthrough_packets(gop_end))
                    continue
                if not found_media:
                    cutters = {
                        stream.index: PassthroughCutter(stream, streams[stream.index], overlap_start, end, offset=overlap_start)
                        for stream in media_container.passthrough_streams
                    }
                found_media = True
                segment = CutSegment(
                    gop.start_time < start or gop_end > end,
                    overlap_start,
                    overlap_end,
                    gop.start_dts,
                    gop.end_dts,
                    gop.index,
                )
                for packet in video_cutter.segment(segment):
                    output.mux(packet)
                for packet in media_container.take_passthrough_packets(overlap_end):
                    cut_packet = cutters[packet.stream_index].packet(packet)
                    if cut_packet is not None:
                        output.mux(cut_packet)
                completed = int((overlap_end - selection[0] - media_container.start_time) * 1_000_000)
                progress.emit(max(0, min(total, completed)), total)
                if overlap_end >= end:
                    break
            for packet in video_cutter.finish():
                output.mux(packet)
        else:
            audio_streams = [s for s in media_container.passthrough_streams if s.type == 'audio']
            if not audio_streams:
                raise ValueError('input has no audio or video streams')
            audio_ends = {
                s.index: ((s.start_time or 0) + s.duration) * s.time_base
                for s in audio_streams if s.duration is not None
            }
            pending_audio = {
                s.index for s in audio_streams
                if (s.start_time is None or s.start_time * s.time_base < end)
                and (s.index not in audio_ends or audio_ends[s.index] > start)
            }
            offset = max(start, media_container.start_time)
            cutters = {
                stream.index: PassthroughCutter(stream, streams[stream.index], start, end, offset=offset)
                for stream in media_container.passthrough_streams
            }
            for packet in media_container.av_container.demux(media_container.streams):
                if not pending_audio:
                    break
                if packet.pts is None:
                    continue
                timestamp = packet.pts * packet.time_base
                if packet.stream.type == 'audio':
                    track_end = audio_ends.get(packet.stream_index)
                    if timestamp >= end or (
                        track_end is not None
                        and (packet.pts + (packet.duration or 0)) * packet.time_base >= track_end
                    ):
                        pending_audio.discard(packet.stream_index)
                if timestamp >= end:
                    continue
                cut_packet = cutters[packet.stream_index].packet(packet)
                if cut_packet is not None:
                    output.mux(cut_packet)
                    found_media = True
                completed = int((timestamp - selection[0] - media_container.start_time) * 1_000_000)
                progress.emit(max(0, min(total, completed)), total)
        if not found_media:
            raise ValueError('selected range contains no media')
        progress.emit(total, total)
