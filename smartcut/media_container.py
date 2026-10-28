from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from math import ceil
from os import cpu_count

from av import Packet, VideoStream
from av import open as av_open
from av import time_base as AV_TIME_BASE
from av.stream import Disposition

from smartcut.nal_tools import (
    get_h264_nal_unit_type,
    get_h265_nal_unit_type,
    get_nal_length_size,
    is_leading_picture_nal_type,
    is_rasl_nal_type,
    is_safe_h265_keyframe_nal,
)


# Leave memory and CPU headroom for mpv and other desktop applications.
CODEC_THREADS = min(4, cpu_count() or 1)


@dataclass
class VideoGop:
    index: int
    packets: list[Packet]
    start_time: Fraction
    start_dts: int
    end_dts: int
    leading_end_dts: int | None
    needs_priming: bool
    is_idr: bool


class MediaContainer:
    def __init__(self, path: str, selection: tuple[Fraction, Fraction]) -> None:
        self.av_container = container = av_open(path, 'r')
        self.start_time = Fraction(container.start_time, AV_TIME_BASE) if container.start_time is not None else Fraction(0)
        self.duration = Fraction(container.duration, AV_TIME_BASE) if container.duration is not None else None
        self.video_stream: VideoStream | None = next((
            stream for stream in container.streams.video
            if Disposition.attached_pic not in stream.disposition
        ), None)
        self.nal_length_size = get_nal_length_size(
            self.video_stream.codec_context.name,
            self.video_stream.codec_context.extradata,
        ) if self.video_stream is not None else 0
        self.passthrough_streams = [
            stream for stream in container.streams
            if stream.type in ('audio', 'subtitle') and stream.time_base
        ]
        self.streams = [*self.passthrough_streams]
        if self.video_stream is not None:
            self.video_stream.thread_type = 'SLICE'
            self.video_stream.codec_context.thread_count = CODEC_THREADS
            self.streams.insert(0, self.video_stream)

        start = selection[0] + self.start_time
        video = self.video_stream
        if video is None:
            if start > self.start_time:
                container.seek(int(start * AV_TIME_BASE))
        # Seeking by video before its first keyframe would skip earlier audio.
        elif start > (video.start_time or 0) * video.time_base:
            # Include the previous GOP for open-GOP decoding references.
            keyframe = self._seek_keyframe(int(start / video.time_base))
            if keyframe is not None:
                self._seek_keyframe(keyframe - 1)

    def _seek_keyframe(self, timestamp: int) -> int | None:
        """Seek before the keyframe at or preceding timestamp and return its timestamp.

        Formats without a keyframe index, such as MPEG-TS, can land anywhere.
        """
        video = self.video_stream
        assert video is not None and video.time_base
        step = ceil(1 / video.time_base)
        target = timestamp
        while True:
            self.av_container.seek(target, stream=video)
            keyframe = next((
                min(packet.pts, packet.dts) if packet.dts is not None else packet.pts
                for packet in self.av_container.demux(video)
                if packet.is_keyframe and packet.pts is not None
            ), None)
            if (keyframe is not None and keyframe <= timestamp) or target <= (video.start_time or 0):
                self.av_container.seek(target, stream=video)
                return keyframe
            target -= step
            step *= 2

    def _gop_boundary(self, packet: Packet) -> bool:
        if not packet.is_keyframe:
            return False
        assert self.video_stream is not None
        codec = self.video_stream.codec_context.name
        if codec == 'hevc':
            return is_safe_h265_keyframe_nal(get_h265_nal_unit_type(bytes(packet), self.nal_length_size))
        return True

    def _gop(self, index: int, packets: list[Packet]) -> VideoGop:
        first, last = packets[0], packets[-1]
        assert first.time_base is not None and last.pts is not None
        leading_end_dts = None
        needs_priming = False
        is_idr = False
        assert self.video_stream is not None
        codec = self.video_stream.codec_context.name
        if codec == 'h264':
            is_idr = get_h264_nal_unit_type(bytes(first), self.nal_length_size) == 5
            needs_priming = not is_idr
        elif codec == 'hevc':
            nal_type = get_h265_nal_unit_type(bytes(first), self.nal_length_size)
            is_idr = nal_type in (19, 20)
            if nal_type == 21:
                has_leading = False
                for packet in packets[1:]:
                    nal_type = get_h265_nal_unit_type(bytes(packet), self.nal_length_size)
                    if not is_leading_picture_nal_type(nal_type):
                        if has_leading:
                            leading_end_dts = packet.dts
                        break
                    has_leading = True
                    needs_priming |= is_rasl_nal_type(nal_type)
        return VideoGop(
            index,
            packets,
            min(packet.pts for packet in packets) * Fraction(first.time_base),
            first.dts if first.dts is not None else -100_000_000,
            last.dts if last.dts is not None else last.pts,
            leading_end_dts,
            needs_priming,
            is_idr,
        )

    def demux(self, end_time: Fraction) -> Iterator[Packet | tuple[VideoGop, Fraction]]:
        """Yield passthrough packets before end_time and video GOPs with their end times.

        Stops reading once audio and video reach end_time. Sparse subtitle tracks
        may have no later cue, and their earlier cues are interleaved before that.
        """
        open_streams = {stream.index for stream in self.streams}
        subtitle_streams = {stream.index for stream in self.streams if stream.type == 'subtitle'}
        previous = None
        packets: list[Packet] = []
        index = 0
        for packet in self.av_container.demux(self.streams):
            if packet.pts is None or packet.stream_index not in open_streams:
                continue
            if packet.stream.type != 'video':
                if packet.pts * packet.time_base < end_time:
                    yield packet
                    continue
            else:
                boundary = self._gop_boundary(packet)
                if not packets and not boundary:
                    # Unindexed seeks can land after a keyframe.
                    continue
                if packets and boundary:
                    gop = self._gop(index, packets)
                    index += 1
                    if previous is not None:
                        yield previous, gop.start_time
                    previous, packets = gop, []
                packets.append(packet)
                if previous is not None and previous.start_time >= end_time:
                    packets = []
                elif packet.dts is not None and packet.dts * packet.time_base >= end_time:
                    # Later packets decode after this one, so they also display after end_time.
                    yield from self._last_gops(previous, index, packets, (packet.pts + 1) * packet.time_base)
                    packets = []
                else:
                    continue
            open_streams.discard(packet.stream_index)
            if open_streams <= subtitle_streams:
                return

        if packets:
            if self.duration is not None:
                end = self.start_time + self.duration + Fraction(1, 10_000)
            else:
                end = max((p.pts + (p.duration or 1)) * p.time_base for p in packets)
            yield from self._last_gops(previous, index, packets, end)

    def _last_gops(
        self, previous: VideoGop | None, index: int, packets: list[Packet], end: Fraction,
    ) -> Iterator[tuple[VideoGop, Fraction]]:
        gop = self._gop(index, packets)
        if previous is not None:
            yield previous, gop.start_time
        yield gop, end

    def close(self) -> None:
        self.av_container.close()
