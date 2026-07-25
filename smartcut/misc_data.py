from dataclasses import dataclass, field
from fractions import Fraction


@dataclass
class AudioExportSettings:
    codec: str

@dataclass
class AudioExportInfo:
    output_tracks: list[AudioExportSettings | None] = field(default_factory=list)

@dataclass
class CutSegment:
    require_recode: bool
    start_time: Fraction
    end_time: Fraction
    gop_start_dts: int = -1
    gop_end_dts: int = -1
    gop_index: int = -1
