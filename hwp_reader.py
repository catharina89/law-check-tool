# -*- coding: utf-8 -*-
"""
HWP / HWPX 텍스트 추출 (외부 전용 라이브러리 없이 구현)
- HWP 5.x : OLE2(CFB) + raw deflate. olefile로 스트림 접근, 레코드 구조 파싱
- HWPX    : ZIP + XML(OWPML). zipfile + ElementTree로 처리
"""
import io
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
import zlib

# HWP 레코드 태그 (HWP 5.0 스펙)
HWPTAG_BEGIN = 0x10
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51   # 67: 문단의 실제 텍스트

# 본문에 섞여있는 제어문자 중, 문단/칸 구분으로 취급할 것
# HWP는 본문 텍스트 안에 인라인 컨트롤 코드(0~31)를 UTF-16 단위로 끼워 넣는다.
INLINE_CTRL_EXTENDED = {
    1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23,
}  # 이들은 뒤에 14워드(28바이트)의 추가 정보가 붙는 '확장 컨트롤'
PARA_BREAK_CTRL = {10, 13}   # 줄바꿈/문단끝
TAB_CTRL = {9}


def _decompress(data):
    """HWP 스트림 압축 해제: raw deflate → zlib → 원본 순으로 시도."""
    if not data:
        return b""
    # HWP는 zlib 헤더 없는 raw deflate를 사용
    try:
        return zlib.decompress(data, -15)
    except zlib.error:
        pass
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    return data  # 비압축 문서


def _parse_para_text(payload):
    """
    HWPTAG_PARA_TEXT 레코드의 payload에서 순수 텍스트를 뽑는다.
    payload는 UTF-16LE 단위들의 배열이며, 값이 0~31인 것은 제어 코드다.
    확장 컨트롤(INLINE_CTRL_EXTENDED)은 뒤에 28바이트 추가 정보가 붙으므로 건너뛴다.
    """
    out = []
    i = 0
    n = len(payload)
    while i + 1 < n:
        code = struct.unpack_from("<H", payload, i)[0]
        if code in PARA_BREAK_CTRL:
            out.append("\n")
            i += 2
        elif code in TAB_CTRL:
            out.append("\t")
            i += 2
        elif code in INLINE_CTRL_EXTENDED:
            # 확장 컨트롤: 본체 2바이트 + 추가 12워드 + 종료 2바이트 = 총 32바이트
            i += 16 * 2
        elif code < 32:
            # 그 외 단순 제어 문자는 무시
            i += 2
        else:
            out.append(chr(code))
            i += 2
    return "".join(out)


def _iter_records(buf):
    """
    HWP 레코드 스트림을 순회한다.
    각 레코드 헤더는 4바이트 little-endian:
      bit 0-9   : tag id
      bit 10-19 : level
      bit 20-31 : size (0xFFF이면 다음 4바이트가 실제 크기)
    """
    i = 0
    n = len(buf)
    while i + 4 <= n:
        header = struct.unpack_from("<I", buf, i)[0]
        tag_id = header & 0x3FF
        size = (header >> 20) & 0xFFF
        i += 4
        if size == 0xFFF:
            if i + 4 > n:
                break
            size = struct.unpack_from("<I", buf, i)[0]
            i += 4
        if i + size > n:
            # 손상된 레코드
            break
        yield tag_id, buf[i:i + size]
        i += size


def extract_text_from_hwp(file_bytes):
    """
    HWP 5.x (OLE2) 바이트에서 텍스트를 추출한다.
    반환: (text, error) — 성공 시 error는 None
    """
    try:
        import olefile
    except ImportError:
        return None, "olefile 라이브러리를 불러오지 못했습니다."

    try:
        if not olefile.isOleFile(io.BytesIO(file_bytes)):
            return None, "올바른 HWP 5.x 파일이 아닙니다 (OLE 구조가 아님)."

        ole = olefile.OleFileIO(io.BytesIO(file_bytes))
        try:
            # FileHeader에서 압축/암호화 여부 확인
            is_compressed = True
            is_encrypted = False
            if ole.exists("FileHeader"):
                fh = ole.openstream("FileHeader").read()
                if len(fh) >= 40:
                    flags = struct.unpack_from("<I", fh, 36)[0]
                    is_compressed = bool(flags & 0x01)
                    is_encrypted = bool(flags & 0x02)

            if is_encrypted:
                return None, "암호가 설정된 HWP 파일입니다. 암호를 해제한 뒤 다시 올려주세요."

            # BodyText/Section* 스트림을 순서대로 모은다
            sections = []
            for entry in ole.listdir():
                if len(entry) >= 2 and entry[0] in ("BodyText", "ViewText"):
                    m = re.match(r"Section(\d+)$", entry[1])
                    if m:
                        sections.append((int(m.group(1)), entry))
            sections.sort(key=lambda x: x[0])

            if not sections:
                return None, "문서 본문(BodyText)을 찾지 못했습니다."

            all_text = []
            for _, entry in sections:
                raw = ole.openstream(entry).read()
                buf = _decompress(raw) if is_compressed else raw
                para_texts = []
                for tag_id, payload in _iter_records(buf):
                    if tag_id == HWPTAG_PARA_TEXT:
                        para_texts.append(_parse_para_text(payload))
                all_text.append("\n".join(para_texts))

            text = "\n".join(all_text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()

            if not text:
                return None, (
                    "텍스트를 추출하지 못했습니다. 문서보안(DRM)이 적용된 파일일 수 있습니다."
                )
            return text, None
        finally:
            ole.close()
    except Exception as e:
        return None, f"HWP 파일 처리 중 오류: {e}"


def extract_text_from_hwpx(file_bytes):
    """
    HWPX (ZIP + XML) 바이트에서 텍스트를 추출한다.
    본문은 Contents/section0.xml, section1.xml ... 에 들어있다.
    반환: (text, error)
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile:
        return None, "올바른 HWPX 파일이 아닙니다 (ZIP 구조가 아님)."

    try:
        names = zf.namelist()
        section_files = []
        for nm in names:
            m = re.search(r"section(\d+)\.xml$", nm, re.IGNORECASE)
            if m:
                section_files.append((int(m.group(1)), nm))
        section_files.sort(key=lambda x: x[0])

        if not section_files:
            # 일부 파일은 구조가 달라 Contents 폴더의 모든 xml을 훑는다
            section_files = [(i, nm) for i, nm in enumerate(names)
                             if nm.lower().endswith(".xml") and "content" in nm.lower()]
            if not section_files:
                return None, "문서 본문(section xml)을 찾지 못했습니다."

        all_text = []
        for _, nm in section_files:
            try:
                data = zf.read(nm)
                root = ET.fromstring(data)
            except Exception:
                continue

            # OWPML은 네임스페이스를 쓰므로 태그 지역명으로 비교한다.
            # 문단(<p>) 단위로 줄을 나누고, 그 안의 텍스트 런(<t>)을 이어붙인다.
            for elem in root.iter():
                local = elem.tag.split("}")[-1].lower()
                if local == "p":
                    parts = []
                    for sub in elem.iter():
                        sub_local = sub.tag.split("}")[-1].lower()
                        if sub_local == "t" and sub.text:
                            parts.append(sub.text)
                    line = "".join(parts).strip()
                    if line:
                        all_text.append(line)

        text = "\n".join(all_text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if not text:
            return None, "HWPX 파일에서 텍스트를 추출하지 못했습니다."
        return text, None
    finally:
        zf.close()


def extract_text_auto(file_bytes, filename=""):
    """확장자/시그니처로 형식을 판별해 적절한 추출기를 호출한다."""
    name = (filename or "").lower()

    # ZIP 시그니처면 hwpx
    if file_bytes[:2] == b"PK":
        return extract_text_from_hwpx(file_bytes)
    # OLE 시그니처면 hwp 5.x
    if file_bytes[:4] == b"\xd0\xcf\x11\xe0":
        return extract_text_from_hwp(file_bytes)

    if name.endswith(".hwpx"):
        return extract_text_from_hwpx(file_bytes)
    if name.endswith(".hwp"):
        return extract_text_from_hwp(file_bytes)

    return None, "HWP/HWPX 형식으로 인식되지 않는 파일입니다."
