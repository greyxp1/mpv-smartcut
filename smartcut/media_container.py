from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from math import ceil
from os import cpu_count

from av import Packet
from av import open as av_open
from av import time_base as AV_TIME_BASE
from av.stream import Disposition


# Leave memory and CPU headroom for mpv and other desktop applications.
CODEC_THREADS = min(4, cpu_count() or 1)


def nal_length_size(codec: str, extradata: bytes | None) -> int:
    """Read AVC/HEVC configuration records, or use Annex B without one."""
    index = {'h264': 4, 'hevc': 21}.get(codec)
    if index is not None and extradata and len(extradata) > index and extradata[0] == 1:
        return (extradata[index] & 3) + 1
    return 0


def _nal_headers(data: bytes, length_size: int, header_size: int) -> Iterator[int]:
    offset = 0
    if length_size:
        while offset + length_size <= len(data):
            size = int.from_bytes(data[offset:offset + length_size], 'big')
            offset += length_size
            if size < header_size or offset + size > len(data):
                return
            yield data[offset]
            offset += size
    else:
        while (start := data.find(b'\0\0\1', offset)) != -1:
            offset = start + 3
            if offset + header_size <= len(data):
                yield data[offset]


def h264_is_idr(data: bytes, length_size: int) -> bool:
    return any(header & 0x1F == 5 for header in _nal_headers(data, length_size, 1))


def h265_nal_type(data: bytes, length_size: int) -> int | None:
    """Prefer BLA/IDR, then CRA, other pictures, and finally metadata."""
    first = picture = None
    cra = False
    for header in _nal_headers(data, length_size, 2):
        nal_type = (header >> 1) & 0x3F
        if 16 <= nal_type <= 20:
            return nal_type
        cra |= nal_type == 21
        if first is None:
            first = nal_type
        if picture is None and nal_type <= 15:
            picture = nal_type
    return 21 if cra else picture if picture is not None else first


@dataclass
class VideoGop:
    packets: list[Packet]
    start_time: Fraction
    # HEVC RADL/RASL pictures directly after a CRA.
    leading_count: int
    needs_priming: bool


class MediaContainer:
    def __init__(self, path: str, selection: tuple[Fraction, Fraction]) -> None:
        self.av_container = container = av_open(path, 'r')
        self.start_time = Fraction(container.start_time, AV_TIME_BASE) if container.start_time is not None else Fraction(0)
        self.duration = Fraction(container.duration, AV_TIME_BASE) if container.duration is not None else None
        self.video_stream = video = next((
            stream for stream in container.streams.video
            if Disposition.attached_pic not in stream.disposition
        ), None)
        self.passthrough_streams = [
            stream for stream in container.streams
            if stream.type in ('audio', 'subtitle') and stream.time_base
        ]
        self.streams = [stream for stream in (video, *self.passthrough_streams) if stream is not None]

        start = selection[0] + self.start_time
        if video is None:
            if start > self.start_time:
                container.seek(int(start * AV_TIME_BASE))
            return
        self.codec = video.codec_context.name
        self.nal_length_size = nal_length_size(self.codec, video.codec_context.extradata)
        video.thread_type = 'SLICE'
        video.codec_context.thread_count = CODEC_THREADS
        # Seeking by video before its first keyframe would skip earlier audio.
        if start > (video.start_time or 0) * video.time_base:
            # Include the previous GOP for open-GOP decoding references.
            keyframe = self._seek_keyframe(int(start / video.time_base))
            if keyframe is not None:
                self._seek_keyframe(keyframe - 1)

    def _seek_keyframe(self, timestamp: int) -> int | None:
        """Seek before the keyframe at or preceding timestamp and return its timestamp.

        Formats without a keyframe index, such as MPEG-TS, can land anywhere.
        """
        video = self.video_stream
        step = ceil(1 / video.time_base)
        target = timestamp
        while True:
            # AVI and FLV reject negative timestamps.
            target = max(target, min(video.start_time or 0, 0))
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

    def _gop(self, packets: list[Packet]) -> VideoGop:
        first = packets[0]
        leading_count = 0
        needs_priming = False
        if self.codec == 'h264':
            needs_priming = not h264_is_idr(bytes(first), self.nal_length_size)
        elif self.codec == 'hevc':
            if h265_nal_type(bytes(first), self.nal_length_size) == 21:
                for packet in packets[1:]:
                    nal_type = h265_nal_type(bytes(packet), self.nal_length_size)
                    if nal_type not in (6, 7, 8, 9):
                        break
                    leading_count += 1
                    # RASL pictures reference frames before the CRA.
                    needs_priming |= nal_type in (8, 9)
        elif self.codec in ('mpeg2video', 'mpeg4'):
            needs_priming = True
        return VideoGop(packets, min(packet.pts for packet in packets) * first.time_base, leading_count, needs_priming)

    def demux(self, end_time: Fraction) -> Iterator[Packet | tuple[VideoGop, Fraction]]:
        """Yield passthrough packets before end_time and video GOPs with their end times.

        Stops reading once audio and video reach end_time. Sparse subtitle tracks
        may have no later cue, and their earlier cues are interleaved before that.
        """
        open_streams = {stream.index for stream in self.streams}
        subtitle_streams = {stream.index for stream in self.streams if stream.type == 'subtitle'}
        previous = end = None
        packets: list[Packet] = []
        for packet in self.av_container.demux(self.streams):
            if packet.pts is None or packet.stream_index not in open_streams:
                continue
            if packet.stream.type != 'video':
                if packet.pts * packet.time_base < end_time:
                    yield packet
                    continue
            else:
                boundary = packet.is_keyframe and (
                    self.codec != 'hevc'
                    or h265_nal_type(bytes(packet), self.nal_length_size) in (None, 16, 17, 18, 19, 20, 21, 32, 33, 34)
                )
                if not packets and not boundary:
                    # Unindexed seeks can land after a keyframe.
                    continue
                if packets and boundary:
                    gop = self._gop(packets)
                    if previous is not None:
                        yield previous, gop.start_time
                    previous, packets = gop, []
                packets.append(packet)
                if previous is not None and previous.start_time >= end_time:
                    packets = []
                elif packet.dts is not None and packet.dts * packet.time_base >= end_time:
                    # Later packets decode after this one, so they also display after end_time.
                    end = (packet.pts + 1) * packet.time_base
                else:
                    continue
            open_streams.discard(packet.stream_index)
            if open_streams <= subtitle_streams:
                break

        if packets:
            if end is None:
                end = self.start_time + self.duration + Fraction(1, 10_000) if self.duration is not None else max(
                    (p.pts + (p.duration or 1)) * p.time_base for p in packets
                )
            gop = self._gop(packets)
            if previous is not None:
                yield previous, gop.start_time
            yield gop, end
