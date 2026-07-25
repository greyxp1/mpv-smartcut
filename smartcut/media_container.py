from dataclasses import dataclass, field
from fractions import Fraction
from typing import cast

from av import AudioStream, Packet, VideoStream
from av import open as av_open
from av import time_base as AV_TIME_BASE
from av.container.input import InputContainer
from av.stream import Stream

from smartcut.nal_tools import (
    get_h264_nal_unit_type,
    get_h265_nal_unit_type,
    is_leading_picture_nal_type,
    is_rasl_nal_type,
    is_safe_h264_keyframe_nal,
    is_safe_h265_keyframe_nal,
)

@dataclass
class AudioTrack:
    av_stream: AudioStream
    packets: list[Packet] = field(default_factory=list)
    frame_times_pts: list[int] = field(default_factory=list)

class MediaContainer:
    av_container: InputContainer
    video_stream: VideoStream | None
    path: str

    gop_start_times_pts_s: list[Fraction] # Smallest pts in a GOP, in seconds

    gop_start_times_dts: list[int]
    gop_end_times_dts: list[int]
    gop_leading_end_dts: list[int | None]  # DTS of first non-leading picture in GOP (None if no leading pics)
    gop_has_rasl: list[bool]  # True if GOP has RASL frames (need priming/hybrid recode)

    audio_tracks: list[AudioTrack]
    subtitle_tracks: list

    duration: Fraction
    start_time: Fraction

    def __init__(self, path: str) -> None:
        self.path = path

        frame_pts = []
        video_keyframe_indices = []

        self.av_container = av_container = av_open(path, 'r', metadata_errors='ignore')

        self.start_time = Fraction(av_container.start_time, AV_TIME_BASE) if av_container.start_time is not None else Fraction(0)
        manual_duration_calc = av_container.duration is None
        self.duration = Fraction(av_container.duration , AV_TIME_BASE) if av_container.duration is not None else Fraction(0)

        is_h264 = False
        is_h265 = False

        streams: list[Stream]

        if len(av_container.streams.video) == 0:
            self.video_stream = None
            streams = [*av_container.streams.audio]
        else:
            self.video_stream = av_container.streams.video[0]
            self.video_stream.thread_type = "FRAME"
            streams = [self.video_stream, *av_container.streams.audio]

            if self.video_stream.codec_context.name == 'hevc':
                is_h265 = True
            if self.video_stream.codec_context.name == 'h264':
                is_h264 = True

        self.audio_tracks = []
        stream_index_to_audio_track = {}
        for audio_stream in av_container.streams.audio:
            if audio_stream.time_base is None:
                continue
            audio_stream.codec_context.thread_type = "FRAME"
            track = AudioTrack(audio_stream)
            self.audio_tracks.append(track)
            stream_index_to_audio_track[audio_stream.index] = track

        self.subtitle_tracks = []
        stream_index_to_subtitle_track = {}
        for i, s in enumerate(av_container.streams.subtitles):
            streams.append(s)
            stream_index_to_subtitle_track[s.index] = i
            self.subtitle_tracks.append([])

        first_keyframe = True  # Always allow the first keyframe regardless of NAL type

        # Track max packet end PTS per stream (integer domain) for manual duration calc
        # Converting to Fraction once at end is much faster than per-packet Fraction math
        max_end_pts_by_stream: dict[int, int] = {}

        self.gop_start_times_dts = []
        self.gop_end_times_dts = []
        self.gop_leading_end_dts = []
        self.gop_has_rasl = []
        last_seen_video_dts = None
        # Track leading pictures in current CRA GOP
        tracking_leading_in_cra = False
        current_gop_has_leading = False
        current_gop_has_rasl = False

        for packet in av_container.demux(streams):
            if packet.pts is None:
                continue

            if manual_duration_calc and (packet.pts is not None and packet.duration is not None):
                stream_idx = packet.stream_index
                end_pts = packet.pts + packet.duration
                if stream_idx not in max_end_pts_by_stream or end_pts > max_end_pts_by_stream[stream_idx]:
                    max_end_pts_by_stream[stream_idx] = end_pts
            if packet.stream.type == 'video' and self.video_stream:

                if packet.is_keyframe:
                    nal_type = None
                    if is_h265:
                        nal_type = get_h265_nal_unit_type(bytes(packet))
                    elif is_h264:
                        nal_type = get_h264_nal_unit_type(bytes(packet))

                    # Always allow the first keyframe regardless of NAL type (may be SEI, parameter sets, etc.)
                    is_safe_keyframe = True
                    if first_keyframe:
                        first_keyframe = False  # Only apply to the very first keyframe
                    # Use centralized helper functions for NAL type safety checks
                    elif is_h265:
                        is_safe_keyframe = is_safe_h265_keyframe_nal(nal_type)
                    elif is_h264:
                        is_safe_keyframe = is_safe_h264_keyframe_nal(nal_type)
                    if is_safe_keyframe:
                        # Finalize previous GOP's leading picture tracking
                        if tracking_leading_in_cra:
                            # Previous GOP was CRA but we never found non-leading picture
                            # This means all frames after CRA were leading (unusual but possible)
                            self.gop_leading_end_dts.append(None if not current_gop_has_leading else last_seen_video_dts)
                            self.gop_has_rasl.append(current_gop_has_rasl)

                        video_keyframe_indices.append(len(frame_pts))
                        dts = packet.dts if packet.dts is not None else -100_000_000
                        self.gop_start_times_dts.append(dts)

                        if last_seen_video_dts is not None:
                            self.gop_end_times_dts.append(last_seen_video_dts)

                        # Start tracking leading pictures if this is a CRA GOP
                        if is_h265 and nal_type == 21:  # CRA frame
                            tracking_leading_in_cra = True
                            current_gop_has_leading = False
                            current_gop_has_rasl = False
                        else:
                            # Not a CRA, no leading pictures to track
                            tracking_leading_in_cra = False
                            current_gop_has_leading = False
                            current_gop_has_rasl = False
                            self.gop_leading_end_dts.append(None)
                            self.gop_has_rasl.append(False)

                elif tracking_leading_in_cra and is_h265:
                    # Check if this non-keyframe packet is a leading picture
                    packet_nal_type = get_h265_nal_unit_type(bytes(packet))
                    if is_leading_picture_nal_type(packet_nal_type):
                        current_gop_has_leading = True
                        if is_rasl_nal_type(packet_nal_type):
                            current_gop_has_rasl = True
                    else:
                        # Found first non-leading picture
                        if current_gop_has_leading:
                            # Record boundary only if there were actual leading pictures
                            dts = packet.dts if packet.dts is not None else -100_000_000
                            self.gop_leading_end_dts.append(dts)
                        else:
                            # No leading pictures in this CRA GOP
                            self.gop_leading_end_dts.append(None)
                        self.gop_has_rasl.append(current_gop_has_rasl)
                        tracking_leading_in_cra = False

                # Use PTS as fallback when DTS is None (common in exported segments)
                last_seen_video_dts = packet.dts if packet.dts is not None else packet.pts
                frame_pts.append(packet.pts)
            elif packet.stream.type == 'audio':
                track = stream_index_to_audio_track[packet.stream_index]
                # NOTE: storing the audio packets like this keeps the whole compressed audio loaded in RAM
                track.packets.append(packet)
            elif packet.stream.type == 'subtitle':
                self.subtitle_tracks[stream_index_to_subtitle_track[packet.stream_index]].append(packet)

        # Finalize manual duration calculation - convert from PTS to Fraction once
        if manual_duration_calc and max_end_pts_by_stream:
            for stream_idx, max_pts in max_end_pts_by_stream.items():
                stream = av_container.streams[stream_idx]
                if stream.time_base is None:
                    continue
                stream_duration = Fraction(max_pts) * stream.time_base
                if stream_duration > self.duration:
                    self.duration = stream_duration

        if self.video_stream is not None:
            # Finalize last GOP's leading picture tracking if still active
            if tracking_leading_in_cra:
                self.gop_leading_end_dts.append(None if not current_gop_has_leading else last_seen_video_dts)
                self.gop_has_rasl.append(current_gop_has_rasl)
            # Ensure gop_end_times_dts has the same length as gop_start_times_dts.
            # This is needed because make_cut_segments uses zip() which truncates to
            # shortest length. When all packets have dts=None (can happen in short
            # exported segments), last_seen_video_dts stays None, so we use the
            # same sentinel value used for gop_start_times_dts when DTS is missing.
            if len(self.gop_end_times_dts) < len(self.gop_start_times_dts):
                fallback_dts = last_seen_video_dts if last_seen_video_dts is not None else -100_000_000
                self.gop_end_times_dts.append(fallback_dts)
            assert len(self.gop_start_times_dts) == len(self.gop_end_times_dts), \
                f"GOP DTS array length mismatch: start={len(self.gop_start_times_dts)}, end={len(self.gop_end_times_dts)}"
            video_frame_times_pts = sorted(frame_pts)

        # Collect integer PTS arrays for audio tracks.
        for t in self.audio_tracks:
            t.frame_times_pts = [cast(int, packet.pts) for packet in t.packets]

        if self.video_stream is not None and self.video_stream.time_base is not None:
            video_time_base = cast(Fraction, self.video_stream.time_base)
            self.gop_start_times_pts_s = [
                Fraction(video_frame_times_pts[index]) * video_time_base
                for index in video_keyframe_indices
            ]

    def close(self) -> None:
        self.av_container.close()
