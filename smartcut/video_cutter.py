from collections.abc import Generator
from dataclasses import dataclass
from fractions import Fraction
from itertools import chain
from typing import cast

import av
import av.bitstream
from av import VideoCodecContext, VideoStream
from av.codec.codec import UnknownCodecError
from av.codec.context import CodecContext
from av.container.output import OutputContainer
from av.packet import Packet
from av.stream import Disposition
from av.video.frame import PictureType, VideoFrame

from smartcut.media_container import CODEC_THREADS, MediaContainer, VideoGop
from smartcut.nal_tools import get_h265_nal_unit_type, is_leading_picture_nal_type


@dataclass
class CutSegment:
    require_recode: bool
    start_time: Fraction
    end_time: Fraction
    gop_start_dts: int = -1
    gop_end_dts: int = -1
    gop_index: int = -1


NVENC_PIX_FMTS = {'yuv420p': 'yuv420p', 'nv12': 'nv12', 'yuv420p10le': 'p010le', 'p010le': 'p010le'}


def is_annexb(packet: Packet | bytes | None) -> bool:
    if packet is None:
        return False
    data = bytes(packet)
    return data[:3] == b'\0\0\x01' or data[:4] == b'\0\0\0\x01'

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


def create_video_output_stream(
    media_container: MediaContainer,
    output_av_container: OutputContainer,
) -> VideoStream:
    in_stream = cast(VideoStream, media_container.video_stream)
    assert in_stream.time_base, "Video stream must have a time_base"

    out_stream = output_av_container.add_stream_from_template(in_stream, opaque=True)
    out_stream.metadata.update(in_stream.metadata)
    out_stream.disposition = cast(Disposition, in_stream.disposition.value)
    out_stream.time_base = in_stream.time_base
    assert out_stream.time_base, "Output stream must have a time_base"
    return out_stream


def _normalize_output_codec_tag(
    out_stream: VideoStream,
    output_av_container: OutputContainer,
    in_stream: VideoStream,
) -> None:
    """Ensure codec tags are compatible with output container format."""
    container_name = output_av_container.format.name.lower() if output_av_container.format.name else ''
    out_codec_ctx = cast(CodecContext, out_stream.codec_context)
    in_codec_ctx = in_stream.codec_context
    in_codec_name = in_codec_ctx.name

    is_mp4_mov_mkv = any(name in container_name for name in ('mp4', 'mov', 'matroska', 'webm'))
    is_mp4_or_mov = any(name in container_name for name in ('mp4', 'mov'))

    # Normalize MPEG-TS codec tags for MP4/MOV/MKV containers
    # Check INPUT stream's codec_tag since output may not have been populated yet
    if is_mp4_mov_mkv and in_codec_name == 'h264' and in_codec_ctx.codec_tag == '\x1b\x00\x00\x00':
        out_codec_ctx.codec_tag = 'avc1'

    # For HEVC in MP4/MOV: use hev1 to keep VPS/SPS/PPS inline
    if is_mp4_or_mov and in_codec_name in ('hevc', 'h265'):
        out_codec_ctx.codec_tag = 'hev1'
    elif is_mp4_mov_mkv and in_codec_name in ('hevc', 'h265') and in_codec_ctx.codec_tag in ('HEVC', '\x24\x00\x00\x00'):
        out_codec_ctx.codec_tag = 'hvc1'


class VideoCutter:
    def __init__(
        self,
        media_container: MediaContainer,
        out_stream: VideoStream,
        output_av_container: OutputContainer,
    ) -> None:
        self.encoder_inited = False

        self.enc_codec = None

        self.in_stream = cast(VideoStream, media_container.video_stream)
        assert self.in_stream.time_base, "Video stream must have a time_base"
        self.in_time_base = Fraction(self.in_stream.time_base)

        self.current_gop: VideoGop | None = None
        self.previous_gop: VideoGop | None = None
        self.demux_iter = iter(())
        self.demux_saved_packet = None

        self.decoder = self.in_stream.codec_context

        self.out_stream = out_stream
        self.codec_name = self.in_stream.codec_context.name
        self.nal_length_size = media_container.nal_length_size

        bitstream_filter = 'null'
        if self.codec_name in ('h264', 'hevc') and not is_annexb(self.in_stream.codec_context.extradata):
            bitstream_filter = f'{self.codec_name}_mp4toannexb'
        elif self.codec_name in {'hevc', 'mpeg4', 'msmpeg4v3', 'msmpeg4v2', 'msmpeg4v1'}:
            # Copied keyframes repeat the source headers. Encoded HEVC boundaries
            # replace parameter sets under the same IDs.
            bitstream_filter = 'dump_extra'
        self.remux_bitstream_filter = av.bitstream.BitStreamFilterContext(bitstream_filter, self.in_stream, self.out_stream)

        # Normalize codec tags for container compatibility (must be done after bitstream filter setup)
        _normalize_output_codec_tag(self.out_stream, output_av_container, self.in_stream)

        assert self.out_stream.time_base, "Output stream must have a time_base"
        self.out_time_base = Fraction(self.out_stream.time_base)

        # Track typical frame duration for fixing missing/zero durations
        # MP4 muxer uses duration to calculate edit list boundaries, so
        # packets without proper duration cause last-frame discard issues
        self.typical_frame_duration: int | None = None

        self.last_dts = -100_000_000
        self.segment_start_in_output = Fraction(0)

        # Track stream continuity for hybrid CRA recoding
        self.last_remuxed_segment_gop_index = None
        self.is_first_remuxed_segment = True

    def prepare_gop(self, gop: VideoGop) -> None:
        self.previous_gop = self.current_gop
        self.current_gop = gop
        previous_packets = self.previous_gop.packets if self.previous_gop is not None else ()
        self.demux_iter = iter(chain(previous_packets, gop.packets))
        self.demux_saved_packet = None

    def init_encoder(self) -> None:
        self.encoder_inited = True
        profile = self.out_stream.codec_context.profile

        codec_name = self.codec_name or ''
        if 'av1' in codec_name:
            self.codec_name = 'av1'
            profile = None
        if self.codec_name == 'vp9':
            if profile is not None:
                profile = profile[-1:]
        elif profile is not None:
            if 'Baseline' in profile:
                profile = 'baseline'
            elif 'High 4:4:4' in profile:
                profile = 'high444'
            elif 'Rext' in profile or 'Simple' in profile: # This is some sort of h265 extension. This might be the source of some issues I've had?
                profile = None
            else:
                profile = profile.lower().replace(':', '').replace(' ', '')

        # Near-lossless boundaries blend with the copied GOPs. Newer codecs are
        # more efficient at the same CRF.
        crf_value = 7 if self.codec_name in ('hevc', 'av1', 'vp9') else 3

        self.encoding_options = {'crf': str(crf_value)}
        if self.codec_name in ('mpeg2video', 'mpeg4', 'msmpeg4v2', 'msmpeg4v3'):
            self.encoding_options = {}
        if self.codec_name in ('h264', 'hevc'):
            self.encoding_options['preset'] = 'ultrafast'
            if self.codec_name == 'hevc':
                self.encoding_options['tune'] = 'zerolatency'
        elif self.codec_name in ('vp8', 'vp9'):
            self.encoding_options.update({
                'deadline': 'good',
                'cpu-used': '4',
                'lag-in-frames': '0',
            })
            if self.codec_name == 'vp9':
                self.encoding_options['row-mt'] = '1'
            else:
                # VP8 requires a bitrate target. Bound quantization so that target
                # cannot reduce boundary quality, and allow CRF below its default min.
                self.encoding_options.update({'qmin': '0', 'qmax': str(crf_value)})
        if self.codec_name == 'av1':
            # Smart cutting only encodes boundary GOPs. Favor low-latency,
            # multithreaded libaom operation while quality remains controlled
            # independently by CRF.
            self.encoding_options.update({
                'cpu-used': '8',
                'row-mt': '1',
                'lag-in-frames': '0',
            })
        if profile is not None:
            self.encoding_options['profile'] = profile

        if self.codec_name == 'h264':
            # sps-id = 3. We try to avoid collisions with the existing SPS ids.
            # Particularly 0 is very commonly used. Technically we should probably try
            # to dynamically set this to a safe number, but it can be difficult to know
            # our detection is robust / correct.
            self.encoding_options['x264-params'] = 'sps-id=3'

        elif self.codec_name == 'hevc':
            # Source encoder settings can override both our speed preset and CRF.
            # Boundary GOPs only need inline headers to join the copied bitstream.
            x265_params = [
                'repeat-headers=1', 'info=0', 'log-level=error',
                f'pools={CODEC_THREADS}', 'frame-threads=1',
            ]
            self.encoding_options['x265-params'] = ':'.join(x265_params)

        # NVENC encodes 4K HEVC and AV1 boundaries about 10-20x faster than
        # software at equal PSNR. AV1 uses a 0-255 quantizer scale.
        self.nvenc_options: dict[str, str] | None = None
        if self.codec_name in ('hevc', 'av1'):
            qp = crf_value * 3 if self.codec_name == 'av1' else crf_value
            self.nvenc_options = {'preset': 'p1', 'bf': '0', 'rc': 'constqp', 'qp': str(qp)}

    def _fix_packet_timestamps(self, packet: Packet) -> None:
        """Fix packet DTS/PTS to ensure monotonic increase and PTS >= DTS."""
        packet.stream = self.out_stream
        packet.time_base = self.out_time_base

        # Treat garbage DTS values as None (can occur during encoder flush due to PyAV
        # reading uninitialized memory when frame is None during flush)
        # See: https://github.com/PyAV-Org/PyAV/issues/397
        #      https://github.com/PyAV-Org/PyAV/discussions/933
        if packet.dts is not None and (packet.dts < -900_000 or packet.dts > 1_000_000_000_000):
            packet.dts = None

        if packet.dts is not None:
            if packet.dts <= self.last_dts:
                packet.dts = self.last_dts + 1
            # Ensure PTS >= DTS (required by all container formats)
            # This check is separate from the monotonicity check above because
            # remux_segment may produce packets with DTS > PTS when adjusting
            # for B-frame delays after an encoded segment.
            if packet.pts is not None and packet.pts < packet.dts:
                packet.pts = packet.dts
            self.last_dts = packet.dts
        if packet.dts is None:
            # When DTS is None, use PTS as fallback (common for keyframes without B-frame reordering)
            # Ensure we don't use the sentinel value to avoid extremely negative DTS
            pts_value = packet.pts if packet.pts is not None else 0
            if self.last_dts < 0:
                # First packet with None DTS, use PTS
                packet.dts = pts_value
            else:
                # Subsequent packets, ensure monotonic increase
                # Don't jump DTS up to match PTS - just increment minimally to preserve PTS >= DTS
                packet.dts = self.last_dts + 1
            self.last_dts = packet.dts

        # Fix packet duration - needed by MP4 muxer to output valid frames
        # Track typical duration from valid packets, use it when duration is missing/zero
        if packet.duration is not None and packet.duration > 0:
            self.typical_frame_duration = packet.duration
        elif self.typical_frame_duration is not None:
            packet.duration = self.typical_frame_duration

    def _ensure_enc_codec(self) -> None:
        """Initialize enc_codec if not already set."""
        if self.enc_codec is not None:
            return

        pix_fmt = self.out_stream.codec_context.pix_fmt
        nvenc_pix_fmt = NVENC_PIX_FMTS.get(pix_fmt) if self.nvenc_options is not None else None
        enc_codec = None
        if nvenc_pix_fmt is not None:
            assert self.nvenc_options is not None
            try:
                enc_codec = self._create_encoder(f'{self.codec_name}_nvenc', self.nvenc_options, nvenc_pix_fmt)
                enc_codec.open()
            except (av.FFmpegError, UnknownCodecError):
                # No NVENC in FFmpeg or no usable NVIDIA GPU or driver; use the software encoder.
                enc_codec = None
                self.nvenc_options = None
        if enc_codec is None:
            enc_codec = self._create_encoder(self.codec_name, self.encoding_options, pix_fmt)
        self.enc_last_pts = -1
        self.enc_codec = enc_codec

    def _create_encoder(self, name: str, options: dict[str, str], pix_fmt: str) -> VideoCodecContext:
        muxing_codec = self.out_stream.codec_context
        enc_codec = cast(VideoCodecContext, CodecContext.create(name, 'w'))

        rate = muxing_codec.rate or self.in_stream.guessed_rate
        if rate:
            enc_codec.rate = rate
        enc_codec.options.update(options)
        if self.codec_name in ('mpeg2video', 'mpeg4', 'msmpeg4v2', 'msmpeg4v3'):
            # Native MPEG encoders use their finest quantizer instead of CRF.
            enc_codec.qscale = True
            enc_codec.global_quality = 118  # FF_QP2LAMBDA

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
        if self.codec_name == 'mpeg2video':
            enc_codec.time_base = 1 / Fraction(muxing_codec.rate)
        else:
            enc_codec.time_base = self.out_time_base

        if self.codec_name in ('vp9', 'av1'):
            # A bitrate target turns CRF into constrained quality for these encoders.
            enc_codec.bit_rate = 0
        elif self.codec_name not in ('h264', 'hevc') and muxing_codec.bit_rate is not None:
            enc_codec.bit_rate = muxing_codec.bit_rate
        if muxing_codec.bit_rate_tolerance is not None:
            enc_codec.bit_rate_tolerance = muxing_codec.bit_rate_tolerance
        enc_codec.codec_tag = muxing_codec.codec_tag
        enc_codec.thread_count = CODEC_THREADS
        enc_codec.thread_type = "FRAME"
        return enc_codec

    def segment(self, cut_segment: CutSegment) -> list[Packet]:
        assert self.current_gop is not None
        # A non-IDR H.264 GOP can only follow the preceding original GOP.
        # After recoding, keep recoding until a complete IDR GOP resets the sequence.
        needs_h264_recode = (
            self.codec_name == 'h264'
            and not self.current_gop.is_idr
            and self.last_remuxed_segment_gop_index != cut_segment.gop_index - 1
        )
        if cut_segment.require_recode or needs_h264_recode:
            packets = self.recode_segment(cut_segment)
        elif self._should_hybrid_recode_cra(cut_segment):
            # CRA GOP with leading pictures: recode leading pics, remux rest
            # Don't flush encoder - leading frames continue into existing encoder
            packets = self.hybrid_recode_cra_segment(cut_segment)
            # Update tracking variables (same as remux path)
            self.last_remuxed_segment_gop_index = cut_segment.gop_index
            self.is_first_remuxed_segment = False
        else:
            packets = self.flush_encoder()
            packets.extend(self.remux_segment(cut_segment))
            # Update tracking variables for hybrid CRA recoding
            self.last_remuxed_segment_gop_index = cut_segment.gop_index
            self.is_first_remuxed_segment = False

        for packet in packets:
            self._fix_packet_timestamps(packet)

        return packets

    def finish(self) -> list[Packet]:
        packets = self.flush_encoder()
        for packet in packets:
            self._fix_packet_timestamps(packet)

        return packets

    def recode_segment(self, s: CutSegment) -> list[Packet]:
        if not self.encoder_inited:
            self.init_encoder()
        result_packets = []

        self._ensure_enc_codec()
        assert self.enc_codec is not None

        # Open-GOP pictures may reference frames before the current keyframe.
        decoder_priming_dts = None
        assert self.current_gop is not None
        if self.previous_gop is not None and self.current_gop.needs_priming:
            decoder_priming_dts = self.previous_gop.start_dts

        for frame in self.fetch_frame(s.gop_start_dts, s.gop_end_dts, s.end_time, decoder_priming_dts):
            assert frame.pts is not None, "Frame pts should not be None after decoding"
            in_tb = frame.time_base or self.in_time_base
            if frame.pts * in_tb < s.start_time:
                continue
            if frame.pts * in_tb >= s.end_time:
                break

            out_tb = self.out_time_base if self.codec_name != 'mpeg2video' else self.enc_codec.time_base

            frame.pts = int(frame.pts - s.start_time / in_tb)

            frame.pts = int(frame.pts * in_tb / out_tb)
            frame.time_base = out_tb
            frame.pts = int(frame.pts + self.segment_start_in_output / out_tb)

            if frame.pts <= self.enc_last_pts:
                frame.pts = int(self.enc_last_pts + 1)
            self.enc_last_pts = frame.pts

            frame.pict_type = PictureType.NONE
            frame = self._scale_frame_if_needed(frame)
            result_packets.extend(self.enc_codec.encode(frame))

        if self.codec_name == 'mpeg2video':
            for p in result_packets:
                p.pts = p.pts * p.time_base / self.out_time_base
                p.dts = p.dts * p.time_base / self.out_time_base
                p.time_base = self.out_time_base
        return result_packets

    def remux_segment(self, s: CutSegment) -> list[Packet]:
        result_packets = []
        segment_start_pts = int(s.start_time / self.in_time_base)

        for packet in self.fetch_packet(s.gop_start_dts, s.gop_end_dts):
            # Apply timing adjustments
            segment_start_offset = self.segment_start_in_output / self.out_time_base
            pts = packet.pts if packet.pts else 0
            packet.pts = int((pts - segment_start_pts) * self.in_time_base / self.out_time_base + segment_start_offset)
            if packet.dts is not None:
                packet.dts = int((packet.dts - segment_start_pts) * self.in_time_base / self.out_time_base + segment_start_offset)

            result_packets.extend(self.remux_bitstream_filter.filter(packet))

        result_packets.extend(self.remux_bitstream_filter.filter(None))

        self.remux_bitstream_filter.flush()
        return result_packets

    def _should_hybrid_recode_cra(self, s: CutSegment) -> bool:
        """
        Check if this segment should use hybrid recode (recode leading pictures only).
        This is needed when:
        1. GOP has RASL frames (they reference frames before the CRA)
        2. There's discontinuity before this point (content was skipped/cut)
        """
        # Check 1: GOP must have RASL frames
        assert self.current_gop is not None
        if self.codec_name != 'hevc' or not self.current_gop.needs_priming:
            return False

        # Check 2: Must have discontinuity before this point
        has_discontinuity = (
            (self.is_first_remuxed_segment and s.gop_index > 0) or
            (self.last_remuxed_segment_gop_index is not None and s.gop_index > self.last_remuxed_segment_gop_index + 1)
        )
        return has_discontinuity

    def hybrid_recode_cra_segment(self, s: CutSegment) -> list[Packet]:
        """
        Hybrid recode for CRA GOPs: recode only leading pictures (RASL/RADL),
        remux CRA + trailing pictures unchanged.
        """
        if not self.encoder_inited:
            self.init_encoder()

        result_packets: list[Packet] = []

        # Same reference point as remux_segment
        segment_start_pts = int(s.start_time / self.in_time_base)
        segment_start_offset = self.segment_start_in_output / self.out_time_base

        # Get leading boundary
        assert self.current_gop is not None
        leading_end_dts = self.current_gop.leading_end_dts
        assert leading_end_dts is not None, "hybrid_recode_cra_segment called without leading pictures"

        self._ensure_enc_codec()
        assert self.enc_codec is not None

        # Decoder priming for RASL reference frames
        decoder_priming_dts = None
        if self.previous_gop is not None:
            decoder_priming_dts = self.previous_gop.start_dts

        # Decode leading + CRA frames, collect packets for later remux
        collected_packets: list[Packet] = []
        all_frames: list[VideoFrame] = []
        gop_start_time = self.current_gop.start_time

        for frame in self.fetch_frame(s.gop_start_dts, leading_end_dts, s.end_time,
                                       decoder_priming_dts, collected_packets):
            if frame.pts is not None and frame.pts * (frame.time_base or self.in_time_base) >= gop_start_time:
                all_frames.append(frame)

        # Get CRA PTS (collected_packets has only non-leading packets, i.e., just CRA)
        assert len(collected_packets) > 0, "No CRA packet found in GOP"
        cra_pts = collected_packets[0].pts
        assert cra_pts is not None, "CRA packet has no PTS"

        # Leading frames precede CRA PTS. Priming frames were discarded while decoding.
        leading_frames = [
            f for f in all_frames
            if f.pts is not None
            and f.pts < cra_pts
        ]
        leading_frames.sort(key=lambda f: f.pts if f.pts is not None else 0)

        # Encode leading frames with same timing formula as remux_segment
        for frame in leading_frames:
            assert frame.pts is not None
            # Same formula as remux_segment
            frame.pts = int((frame.pts - segment_start_pts) * self.in_time_base / self.out_time_base + segment_start_offset)
            frame.time_base = self.out_time_base

            if frame.pts <= self.enc_last_pts:
                frame.pts = int(self.enc_last_pts + 1)
            self.enc_last_pts = frame.pts

            frame.pict_type = PictureType.NONE
            result_packets.extend(self.enc_codec.encode(frame))

        result_packets.extend(self.flush_encoder())

        # Fix encoder DTS
        for p in result_packets:
            if p.dts is None or p.dts > 1_000_000_000_000:
                p.dts = p.pts

        # Remux: collected packets (CRA, leading already filtered in fetch_frame) + trailing
        # Handle in one loop with same timing as remux_segment
        remux_packets = list(collected_packets)
        remux_packets.extend(self.fetch_packet(leading_end_dts, s.gop_end_dts))

        for packet in remux_packets:
            pts = packet.pts if packet.pts else 0
            packet.pts = int((pts - segment_start_pts) * self.in_time_base / self.out_time_base + segment_start_offset)
            if packet.dts is not None:
                packet.dts = int((packet.dts - segment_start_pts) * self.in_time_base / self.out_time_base + segment_start_offset)
            result_packets.extend(self.remux_bitstream_filter.filter(packet))

        result_packets.extend(self.remux_bitstream_filter.filter(None))
        self.remux_bitstream_filter.flush()

        return result_packets

    def flush_encoder(self) -> list[Packet]:
        if self.enc_codec is None:
            return []

        result_packets = self.enc_codec.encode()

        if self.codec_name == 'mpeg2video':
            for p in result_packets:
                if p.time_base:
                    if p.pts is not None:
                        p.pts = int(p.pts * p.time_base / self.out_time_base)
                    if p.dts is not None:
                        p.dts = int(p.dts * p.time_base / self.out_time_base)
                p.time_base = self.out_time_base

        self.enc_codec = None
        return result_packets

    def _scale_frame_if_needed(self, frame: VideoFrame) -> VideoFrame:
        """Scale frame to encoder dimensions if needed, using bilinear interpolation.

        Stays in the same pixel format (no YUV->RGB conversion).
        """
        if self.enc_codec is None:
            return frame

        if frame.width == self.enc_codec.width and frame.height == self.enc_codec.height:
            return frame

        # reformat() preserves pts/time_base and keeps pixel format unchanged
        return frame.reformat(
            width=self.enc_codec.width,
            height=self.enc_codec.height,
            interpolation='BILINEAR'
        )

    def fetch_packet(self, target_dts: int, end_dts: int) -> Generator[Packet, None, None]:
        # First, check if we have a saved packet from previous call
        if self.demux_saved_packet is not None:
            saved_dts = self.demux_saved_packet.dts if self.demux_saved_packet.dts is not None else -100_000_000
            if saved_dts >= target_dts:
                if saved_dts <= end_dts:
                    packet = self.demux_saved_packet
                    self.demux_saved_packet = None
                    yield copy_packet(packet)
                else:
                    # Saved packet is beyond our end range, don't yield it
                    return
            else:
                # Saved packet is before our target, clear it
                self.demux_saved_packet = None

        for packet in self.demux_iter:
            in_dts = packet.dts if packet.dts is not None else -100_000_000

            # Skip packets before target_dts
            if packet.pts is None or in_dts < target_dts:
                continue

            # Check if packet exceeds end_dts
            if in_dts > end_dts:
                # Save this packet for next call and stop iteration
                self.demux_saved_packet = packet
                return

            # Packet is in our target range, yield it
            yield copy_packet(packet)

    def fetch_frame(self, gop_start_dts: int, gop_end_dts: int, end_time: Fraction, decoder_priming_dts: int | None = None, collect_packets: list[Packet] | None = None) -> Generator[VideoFrame, None, None]:
        start_dts = decoder_priming_dts if decoder_priming_dts is not None else gop_start_dts
        self.decoder.flush_buffers()

        # The decoder reorders pictures. Yield them directly instead of retaining 15 full frames.
        for packet in chain(self.fetch_packet(start_dts, gop_end_dts), (None,)):
            if packet is not None and collect_packets is not None:
                packet_dts = packet.dts if packet.dts is not None else packet.pts
                should_collect = packet_dts is not None and packet_dts >= gop_start_dts
                if should_collect and self.codec_name == 'hevc':
                    nal_type = get_h265_nal_unit_type(bytes(packet), self.nal_length_size)
                    if is_leading_picture_nal_type(nal_type):
                        should_collect = False
                if should_collect:
                    collect_packets.append(copy_packet(packet))

            try:
                frames = self.decoder.decode(packet)
            except av.error.EOFError:
                if packet is not None:
                    raise
                return
            for frame in frames:
                if frame.pts is not None and frame.pts * (frame.time_base or self.in_time_base) >= end_time:
                    return
                yield frame
