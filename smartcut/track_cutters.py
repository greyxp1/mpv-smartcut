from fractions import Fraction
from math import ceil, floor
from typing import cast

from av.container.output import OutputContainer
from av.packet import Packet
from av.stream import Disposition, Stream


def create_output_stream(output: OutputContainer, stream: Stream) -> Stream:
    out_stream = output.add_stream_from_template(stream)
    out_stream.metadata.update(stream.metadata)
    out_stream.disposition = cast(Disposition, stream.disposition.value)
    return out_stream


class PassthroughCutter:
    def __init__(self, in_stream: Stream, out_stream: Stream, start: Fraction, end: Fraction, *, offset: Fraction) -> None:
        assert in_stream.time_base
        self.out_stream = out_stream
        offset_pts = offset / in_stream.time_base
        self.offset_floor = floor(offset_pts)
        self.offset_ceil = ceil(offset_pts)
        self.start_pts = ceil(start / in_stream.time_base)
        self.end_pts = ceil(end / in_stream.time_base)
        self.last_pts = -100_000
        self.last_dts = -100_000

    def packet(self, packet: Packet) -> Packet | None:
        if packet.pts is None:
            return None
        start = packet.pts
        if packet.stream.type == 'subtitle' and packet.duration:
            # Keep cues overlapping the selection and clip them to it.
            start = max(start, self.start_pts)
            end = min(packet.pts + packet.duration, self.end_pts)
            if start >= end:
                return None
            packet.duration = end - start
        elif not self.start_pts <= start < self.end_pts:
            return None
        elif packet.dts is None and packet.stream.type == 'audio':
            return None
        # Truncate the shifted timestamp toward zero without per-packet fractions.
        pts = start - (self.offset_ceil if start >= self.offset_ceil else self.offset_floor)
        if packet.stream.type == 'subtitle':
            packet.pts = max(pts, self.last_pts)
            packet.dts = packet.pts
        else:
            dts = packet.dts - (self.offset_ceil if packet.dts >= self.offset_ceil else self.offset_floor)
            packet.pts = max(pts, self.last_pts + 1)
            packet.dts = max(dts, self.last_dts + 1)
        self.last_pts = packet.pts
        self.last_dts = packet.dts
        packet.stream = self.out_stream
        return packet
