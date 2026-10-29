from collections.abc import Iterator
from fractions import Fraction
from itertools import chain

import av
import av.bitstream
from av import VideoCodecContext
from av.codec.codec import UnknownCodecError
from av.codec.context import CodecContext
from av.container.output import OutputContainer
from av.packet import Packet
from av.video.frame import PictureType, VideoFrame

from smartcut.media_container import CODEC_THREADS, MediaContainer, VideoGop
from smartcut.track_cutters import create_output_stream


NVENC_PIX_FMTS = {'yuv420p': 'yuv420p', 'yuv420p10le': 'p010le'}


def copy_packet(p: Packet) -> Packet:
    packet = Packet(p)
    packet.pts = p.pts
    packet.dts = p.dts
    packet.duration = p.duration
    packet.time_base = p.time_base
    packet.stream = p.stream
    packet.is_keyframe = p.is_keyframe
    for side_data in p.iter_sidedata():
        packet.set_sidedata(side_data)
    return packet


class VideoCutter:
    def __init__(self, media_container: MediaContainer, output: OutputContainer, origin: Fraction) -> None:
        self.in_stream = media_container.video_stream
        self.out_stream = create_output_stream(output, self.in_stream)
        # The output stream copies the input time base.
        self.time_base = Fraction(self.in_stream.time_base)
        self.decoder = self.in_stream.codec_context
        # Decoders such as libdav1d have their own names.
        self.codec_name = self.decoder.codec.canonical_name

        bitstream_filter = 'null'
        if media_container.nal_length_size:
            bitstream_filter = f'{self.codec_name}_mp4toannexb'
        elif self.codec_name in {'hevc', 'mpeg4', 'msmpeg4v3', 'msmpeg4v2', 'msmpeg4v1'}:
            # Copied keyframes repeat the source headers. Encoded HEVC boundaries
            # replace parameter sets under the same IDs.
            bitstream_filter = 'dump_extra'
        self.remux_bitstream_filter = av.bitstream.BitStreamFilterContext(bitstream_filter, self.in_stream, self.out_stream)

        # Codec tags must be set after the bitstream filter.
        if self.codec_name == 'hevc' and output.format.name in ('mp4', 'mov'):
            # hev1 allows the parameter sets of encoded boundaries inline.
            self.out_stream.codec_context.codec_tag = 'hev1'
        elif self.decoder.codec_tag == '\x1b\x00\x00\x00':
            # MPEG-TS H.264, which Matroska and MP4 reject
            self.out_stream.codec_context.codec_tag = 'avc1'

        self.current_gop: VideoGop | None = None
        self.previous_gop: VideoGop | None = None
        self.last_remuxed_gop: VideoGop | None = None
        self.has_output_frames = False
        # Output time zero in input time, shared with the passthrough streams.
        self.origin = origin
        self.shift = origin / self.time_base
        self.enc_codec: VideoCodecContext | None = None
        self.last_dts = -100_000_000
        self.frame_duration: int | None = None

        # Near-lossless boundaries blend with the copied GOPs. Newer codecs are
        # more efficient at the same CRF. Native MPEG encoders use their finest
        # quantizer instead, and VP8 bounds it so its bitrate target can't lower quality.
        qscale = {'flags': '+qscale', 'global_quality': '118'}
        vpx = {'deadline': 'good', 'cpu-used': '4', 'lag-in-frames': '0'}
        self.encoding_options = {
            # Avoid colliding with the commonly used SPS id 0.
            'h264': {'crf': '3', 'preset': 'ultrafast', 'x264-params': 'sps-id=3'},
            # Source encoder settings can override both our speed preset and CRF.
            # Boundary GOPs only need inline headers to join the copied bitstream.
            'hevc': {
                'crf': '7', 'preset': 'ultrafast', 'tune': 'zerolatency',
                'x265-params': f'repeat-headers=1:info=0:log-level=error:pools={CODEC_THREADS}:frame-threads=1',
            },
            'vp8': {'crf': '3', **vpx, 'qmin': '0', 'qmax': '3'},
            'vp9': {'crf': '7', **vpx, 'row-mt': '1'},
            'av1': {'crf': '7', 'cpu-used': '8', 'row-mt': '1', 'lag-in-frames': '0'},
            'mpeg2video': qscale,
            # B-frames clear the in-band VOL low_delay flag like the copied GOPs.
            # Otherwise the decoder repeats a frame at each join.
            'mpeg4': {**qscale, 'bf': '1'} if self.decoder.has_b_frames else qscale,
            'msmpeg4v2': qscale,
            'msmpeg4v3': qscale,
        }.get(self.codec_name, {})
        # NVENC encodes 4K HEVC and AV1 boundaries about 10-20x faster than
        # software at equal PSNR. AV1 uses a 0-255 quantizer scale.
        self.nvenc_options: dict[str, str] | None = None
        if self.codec_name in ('hevc', 'av1'):
            qp = '21' if self.codec_name == 'av1' else '7'
            self.nvenc_options = {'preset': 'p1', 'bf': '0', 'rc': 'constqp', 'qp': qp}

    def prepare_gop(self, gop: VideoGop) -> None:
        self.previous_gop, self.current_gop = self.current_gop, gop

    def segment(self, start: Fraction, end: Fraction, cuts_end: bool) -> list[Packet]:
        gop, previous = self.current_gop, self.previous_gop
        follows_remux = previous is not None and self.last_remuxed_gop is previous
        keyframe_time = gop.packets[0].pts * self.time_base
        # HEVC leading pictures display before their CRA and can be recoded on their own,
        # when the cut drops some of them or RASL pictures lost their references.
        recode_leading = self.codec_name == 'hevc' and gop.leading_count > 0 and start <= keyframe_time and (
            gop.start_time < start or (gop.needs_priming and previous is not None and not follows_remux)
        )
        if (
            cuts_end
            or (gop.start_time < start and not recode_leading)
            # A non-IDR H.264 GOP can only follow its original predecessor. After
            # recoding, keep recoding until an IDR GOP resets the sequence.
            or (self.codec_name == 'h264' and gop.needs_priming and not follows_remux)
            # Leading B-frames of an open MPEG GOP need the previous GOP's last frame in the output.
            or (self.codec_name in ('mpeg2video', 'mpeg4') and gop.needs_priming
                and previous is not None and not self.has_output_frames)
        ):
            return self._fix_timestamps(self._recode(start, end))
        if recode_leading:
            packets = self._recode_leading(start, end, keyframe_time)
        else:
            packets = self.flush_encoder() + self._remux(gop.packets)
        self.last_remuxed_gop = gop
        self.has_output_frames = True
        return self._fix_timestamps(packets)

    def finish(self) -> list[Packet]:
        return self._fix_timestamps(self.flush_encoder())

    def _recode(self, start: Fraction, end: Fraction) -> list[Packet]:
        enc_codec = self._ensure_encoder()
        time_base = Fraction(enc_codec.time_base)
        gop, previous = self.current_gop, self.previous_gop
        # Open-GOP pictures may reference frames before the current keyframe.
        packets = [*previous.packets, *gop.packets] if previous is not None and gop.needs_priming else gop.packets
        result = []
        for frame in self._decode(packets, end):
            time = frame.pts * frame.time_base
            if time >= start:
                result += self._encode_at(frame, round((time - self.origin) / time_base))
                self.has_output_frames = True
        return result

    def _recode_leading(self, start: Fraction, end: Fraction, keyframe_time: Fraction) -> list[Packet]:
        """Recode the selected RASL/RADL pictures of a CRA GOP and copy the CRA and trailing pictures.

        HEVC places leading pictures right after their CRA in decoding order.
        """
        gop, previous = self.current_gop, self.previous_gop
        count = gop.leading_count + 1
        result = []
        if start < keyframe_time:
            self._ensure_encoder()
            priming = previous.packets if previous is not None and gop.needs_priming else []
            for frame in self._decode([*priming, *gop.packets[:count]], end):
                if start <= frame.pts * frame.time_base < keyframe_time:
                    result += self._encode_at(frame, int(frame.pts - self.shift))
        return result + self.flush_encoder() + self._remux([gop.packets[0], *gop.packets[count:]])

    def _remux(self, packets: list[Packet]) -> list[Packet]:
        result = []
        # The bitstream filter takes ownership of packets that may be decoded again.
        for packet in map(copy_packet, packets):
            packet.pts = int(packet.pts - self.shift)
            if packet.dts is not None:
                packet.dts = int(packet.dts - self.shift)
            result += self.remux_bitstream_filter.filter(packet)
        result += self.remux_bitstream_filter.filter(None)
        self.remux_bitstream_filter.flush()
        return result

    def _decode(self, packets: list[Packet], end: Fraction) -> Iterator[VideoFrame]:
        self.decoder.flush_buffers()
        # The decoder reorders pictures. Yield them directly instead of retaining full frames.
        for packet in chain(packets, (None,)):
            for frame in self.decoder.decode(packet):
                if frame.pts * frame.time_base >= end:
                    return
                yield frame

    def _encode_at(self, frame: VideoFrame, pts: int) -> list[Packet]:
        frame.pts = self.enc_last_pts = max(pts, self.enc_last_pts + 1)
        frame.time_base = self.enc_codec.time_base
        frame.pict_type = PictureType.NONE
        return self._encode(frame)

    def _encode(self, frame: VideoFrame | None) -> list[Packet]:
        packets = self.enc_codec.encode(frame)
        if self.codec_name == 'mpeg2video':
            # The MPEG-2 encoder uses a frame-rate time base.
            for p in packets:
                p.pts = round(p.pts * p.time_base / self.time_base)
                if p.dts is not None:
                    p.dts = round(p.dts * p.time_base / self.time_base)
                p.time_base = self.time_base
        return packets

    def flush_encoder(self) -> list[Packet]:
        if self.enc_codec is None:
            return []
        packets = self._encode(None)
        self.enc_codec = None
        return packets

    def _fix_timestamps(self, packets: list[Packet]) -> list[Packet]:
        """Keep DTS increasing and PTS >= DTS across copied and encoded packets."""
        for packet in packets:
            packet.stream = self.out_stream
            packet.time_base = self.time_base
            if packet.dts is None:
                packet.dts = packet.pts if self.last_dts < 0 else self.last_dts + 1
            elif packet.dts <= self.last_dts:
                packet.dts = self.last_dts + 1
            packet.pts = max(packet.pts, packet.dts)
            self.last_dts = packet.dts
            # The MP4 muxer drops the last frame of packets without a duration.
            if packet.duration:
                self.frame_duration = packet.duration
            elif self.frame_duration is not None:
                packet.duration = self.frame_duration
        return packets

    def _ensure_encoder(self) -> VideoCodecContext:
        if self.enc_codec is None:
            self.enc_last_pts = -1
            pix_fmt = self.out_stream.codec_context.pix_fmt
            nvenc_pix_fmt = NVENC_PIX_FMTS.get(pix_fmt)
            if self.nvenc_options is not None and nvenc_pix_fmt is not None:
                try:
                    self.enc_codec = self._create_encoder(f'{self.codec_name}_nvenc', self.nvenc_options, nvenc_pix_fmt)
                    self.enc_codec.open()
                    return self.enc_codec
                except (av.FFmpegError, UnknownCodecError):
                    # No NVENC in FFmpeg or no usable NVIDIA GPU or driver.
                    self.nvenc_options = None
            self.enc_codec = self._create_encoder(self.codec_name, self.encoding_options, pix_fmt)
        return self.enc_codec

    def _create_encoder(self, name: str, options: dict[str, str], pix_fmt: str) -> VideoCodecContext:
        muxing_codec = self.out_stream.codec_context
        enc_codec = CodecContext.create(name, 'w')
        rate = muxing_codec.rate or self.in_stream.guessed_rate
        if rate:
            enc_codec.rate = rate
        enc_codec.options.update(options)
        enc_codec.width = muxing_codec.width
        enc_codec.height = muxing_codec.height
        enc_codec.pix_fmt = pix_fmt
        enc_codec.color_range = muxing_codec.color_range
        enc_codec.colorspace = muxing_codec.colorspace
        enc_codec.color_primaries = muxing_codec.color_primaries
        enc_codec.color_trc = muxing_codec.color_trc
        enc_codec.chroma_sample_location = muxing_codec.chroma_sample_location
        if muxing_codec.sample_aspect_ratio:
            enc_codec.sample_aspect_ratio = muxing_codec.sample_aspect_ratio
        enc_codec.time_base = 1 / Fraction(rate) if self.codec_name == 'mpeg2video' else self.time_base
        if self.codec_name in ('vp9', 'av1'):
            # A bitrate target turns CRF into constrained quality for these encoders.
            enc_codec.bit_rate = 0
        elif self.codec_name not in ('h264', 'hevc') and muxing_codec.bit_rate is not None:
            enc_codec.bit_rate = muxing_codec.bit_rate
        enc_codec.codec_tag = muxing_codec.codec_tag
        enc_codec.thread_count = CODEC_THREADS
        enc_codec.thread_type = 'FRAME'
        return enc_codec
