"""구형 .hwp(HWP 5.0 바이너리) 텍스트 추출.

읽기 전용이다. 이 형식은 서식을 보존한 채 안전하게 치환하기 어려워서,
비식별 사본은 마크다운으로만 낸다. 서식이 필요하면 한글에서 .hwpx 로 저장한
뒤 다시 넣도록 안내한다.

구조: OLE 복합문서의 BodyText/Section* 스트림(대개 zlib 압축) 안에
레코드가 이어지고, HWPTAG_PARA_TEXT(51) 레코드의 본문이 UTF-16LE 다.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from ..errors import DocumentRejected, UnsupportedFormat
from ..model import KIND_PARA, Piece, Segment

HWPTAG_BEGIN = 0x10
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51

# 본문 안의 제어문자 — 표·그림 등 개체 자리를 나타낸다
INLINE_CONTROLS = set(range(1, 32)) - {9, 10, 13}
EXTENDED_CONTROLS = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}


def _olefile():
    try:
        import olefile
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedFormat(".hwp 처리에는 olefile 이 필요합니다") from exc
    return olefile


def _iter_records(data: bytes):
    pos, size = 0, len(data)
    while pos + 4 <= size:
        header = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag_id = header & 0x3FF
        length = (header >> 20) & 0xFFF
        if length == 0xFFF:
            if pos + 4 > size:
                break
            length = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        yield tag_id, data[pos:pos + length]
        pos += length


def _decode_para_text(payload: bytes) -> str:
    out: list[str] = []
    idx, size = 0, len(payload) - 1
    while idx < size:
        code = struct.unpack_from("<H", payload, idx)[0]
        idx += 2
        if code in EXTENDED_CONTROLS:
            idx += 14          # 확장 제어문자는 16바이트를 차지한다
            continue
        if code in INLINE_CONTROLS:
            continue
        out.append(chr(code))
    return "".join(out)


def extract(path: Path) -> list[Segment]:
    olefile = _olefile()
    if not olefile.isOleFile(str(path)):
        raise DocumentRejected("HWP 5.0 형식이 아닙니다. 한글에서 .hwpx 로 저장해 주십시오.")

    ole = olefile.OleFileIO(str(path))
    try:
        compressed = True
        if ole.exists("FileHeader"):
            header = ole.openstream("FileHeader").read()
            if len(header) > 37:
                compressed = bool(header[36] & 0x01)
                if header[36] & 0x02:
                    raise DocumentRejected(
                        "암호가 걸린 문서입니다. 한글에서 암호를 풀고 다시 시도하십시오.")

        streams = sorted(
            (entry for entry in ole.listdir() if entry[0] == "BodyText"),
            key=lambda e: e[-1],
        )
        if not streams:
            raise DocumentRejected("본문 스트림을 찾지 못했습니다.")

        segments: list[Segment] = []
        order = 0
        for entry in streams:
            raw = ole.openstream(entry).read()
            data = zlib.decompress(raw, -15) if compressed else raw
            part = "/".join(entry)
            for tag_id, payload in _iter_records(data):
                if tag_id != HWPTAG_PARA_TEXT:
                    continue
                text = _decode_para_text(payload)
                if not text.strip():
                    continue
                segments.append(Segment(
                    seg_id=f"{part}:{order:05d}", text=text, kind=KIND_PARA,
                    part=part, order=order,
                    pieces=[Piece(node=None, slot="line", start=0, length=len(text))],
                ))
                order += 1
        return segments
    finally:
        ole.close()
