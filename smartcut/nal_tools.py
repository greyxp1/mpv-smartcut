from collections.abc import Iterator


def get_nal_length_size(codec: str, extradata: bytes | None) -> int:
    """Read AVC/HEVC configuration records, or use Annex B without one."""
    index = 4 if codec == 'h264' else 21 if codec == 'hevc' else None
    if index is not None and extradata and len(extradata) > index and extradata[0] == 1:
        return (extradata[index] & 3) + 1
    return 0


def _nal_headers(data: bytes, length_size: int | None, header_size: int) -> Iterator[int]:
    if length_size is None:
        length_size = 0 if data.startswith((b'\0\0\1', b'\0\0\0\1')) else 4
    if length_size:
        offset = 0
        while offset + length_size <= len(data):
            size = int.from_bytes(data[offset:offset + length_size], 'big')
            offset += length_size
            if size < header_size or offset + size > len(data):
                return
            yield data[offset]
            offset += size
    else:
        offset = 0
        while (start := data.find(b'\0\0\1', offset)) != -1:
            offset = start + 3
            if offset + header_size <= len(data):
                yield data[offset]


def _nal_unit_type(data: bytes, length_size: int | None, *, hevc: bool) -> int | None:
    first = picture = cra = None
    for header in _nal_headers(data, length_size, 2 if hevc else 1):
        nal_type = (header >> 1) & 0x3F if hevc else header & 0x1F
        if first is None:
            first = nal_type
        if (hevc and 16 <= nal_type <= 20) or (not hevc and nal_type == 5):
            return nal_type
        if hevc and nal_type == 21:
            cra = nal_type
        if picture is None and ((hevc and nal_type <= 15) or (not hevc and 1 <= nal_type <= 4)):
            picture = nal_type
    return cra if cra is not None else picture if picture is not None else first


def get_h265_nal_unit_type(packet_data: bytes, length_size: int | None = None) -> int | None:
    """Prefer BLA/IDR, then CRA, other pictures, and finally metadata."""
    return _nal_unit_type(packet_data, length_size, hevc=True)


def get_h264_nal_unit_type(packet_data: bytes, length_size: int | None = None) -> int | None:
    """Prefer IDR, then other pictures, and finally metadata."""
    return _nal_unit_type(packet_data, length_size, hevc=False)


def is_safe_h265_keyframe_nal(nal_type: int | None) -> bool:
    return nal_type is None or nal_type in (16, 17, 18, 19, 20, 21, 32, 33, 34)


def is_rasl_nal_type(nal_type: int | None) -> bool:
    """RASL pictures reference frames before their associated CRA."""
    return nal_type in (8, 9)


def is_leading_picture_nal_type(nal_type: int | None) -> bool:
    """RADL and RASL pictures precede their IRAP in presentation order."""
    return nal_type in (6, 7, 8, 9)
