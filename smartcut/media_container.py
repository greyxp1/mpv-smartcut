from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from math import ceil
from os import cpu_count

from av import Packet, VideoStream
from av import open as av_open
from av import time_base as AV_TIME_BASE

from smartcut.nal_tools import (
    get_h264_nal_unit_type,
    get_h265_nal_unit_type,
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
        self.video_stream: VideoStream | None = container.streams.video[0] if container.streams.video else None
        self.passthrough_streams = [
            stream for stream in container.streams
            if stream.type in ('audio', 'subtitle') and stream.time_base
        ]
        self.passthrough_packets: list[Packet] = []
        self.streams = [*self.passthrough_streams]
        if self.video_stream is not None:
            self.video_stream.thread_type = 'SLICE'
            self.video_stream.codec_context.thread_count = CODEC_THREADS
            self.streams.insert(0, self.video_stream)

        start = selection[0] + self.start_time
        if start > self.start_time:
            if self.video_stream is not None:
                # Include the previous GOP for open-GOP decoding references.
                keyframe = self._seek_keyframe(int(start / self.video_stream.time_base))
                if keyframe is not None:
                    self._seek_keyframe(keyframe - 1)
            else:
                container.seek(int(start * AV_TIME_BASE))

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
            if keyframe is None or keyframe <= timestamp or target <= (video.start_time or 0):
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
            return is_safe_h265_keyframe_nal(get_h265_nal_unit_type(bytes(packet)))
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
            is_idr = get_h264_nal_unit_type(bytes(first)) == 5
            needs_priming = not is_idr
        elif codec == 'hevc':
            nal_type = get_h265_nal_unit_type(bytes(first))
            is_idr = nal_type in (19, 20)
            if nal_type == 21:
                has_leading = False
                for packet in packets[1:]:
                    nal_type = get_h265_nal_unit_type(bytes(packet))
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

    def gops(self, end_time: Fraction) -> Iterator[tuple[VideoGop, Fraction]]:
        pending = None
        packets: list[Packet] = []
        index = 0
        for packet in self.av_container.demux(self.streams):
            if packet.pts is None:
                continue
            if packet.stream.type != 'video':
                self.passthrough_packets.append(packet)
                continue
            if not packets and not self._gop_boundary(packet):
                # Unindexed seeks can land after a keyframe.
                continue
            if packets and self._gop_boundary(packet):
                gop = self._gop(index, packets)
                index += 1
                if pending is not None:
                    yield pending, gop.start_time
                    if gop.start_time >= end_time:
                        return
                pending = gop
                packets = []
            packets.append(packet)

        if packets:
            gop = self._gop(index, packets)
            if pending is not None:
                yield pending, gop.start_time
                if gop.start_time >= end_time:
                    return
            if self.duration is not None:
                end = self.start_time + self.duration + Fraction(1, 10_000)
            else:
                end = max((p.pts + (p.duration or 1)) * p.time_base for p in packets)
            yield gop, end

    def take_passthrough_packets(self, end_time: Fraction) -> Iterator[Packet]:
        end_pts = {
            stream.index: ceil(end_time / stream.time_base)
            for stream in self.passthrough_streams
        }
        packets, self.passthrough_packets = self.passthrough_packets, []
        for packet in packets:
            if packet.pts < end_pts[packet.stream_index]:
                yield packet
            else:
                self.passthrough_packets.append(packet)

    def close(self) -> None:
        self.av_container.close()
