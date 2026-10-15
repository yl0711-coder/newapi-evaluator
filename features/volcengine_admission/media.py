"""Bounded MP4 video-track validation and metadata removal, without claiming decode.

Support is deliberately limited to unfragmented, self-contained H.264/H.265 video.
Unknown metadata is replaced with equal-size zero-filled free boxes, preserving
sample offsets. Codec SEI metadata is replaced with equal-size filler NAL units.
"""
from dataclasses import dataclass
import struct

from .privacy import PrivacyScanLimit, media_contains_caller_text


class MediaError(ValueError):
    pass


@dataclass
class Box:
    start: int
    data: int
    end: int
    kind: bytes


def boxes(raw, start, end):
    result = []
    while start < end:
        if len(result) > 10000 or start + 8 > end:
            raise MediaError("media_track_invalid")
        size, kind = struct.unpack_from(">I4s", raw, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise MediaError("media_track_invalid")
            size, header = int.from_bytes(raw[start + 8:start + 16], "big"), 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise MediaError("media_track_invalid")
        result.append(Box(start, start + header, start + size, kind))
        start += size
    return result


def only(rows, kind):
    found = [box for box in rows if box.kind == kind]
    if len(found) != 1:
        raise MediaError("media_track_invalid")
    return found[0]


class MP4:
    def __init__(self, raw):
        self.raw, self.clean = raw, bytearray(raw)
        self.ranges = []
        self.samples = []

    def zero(self, box):
        self.clean[box.start + 4:box.start + 8] = b"free"
        self.clean[box.data:box.end] = bytes(box.end - box.data)

    def children(self, box, allowed):
        rows = boxes(self.raw, box.data, box.end)
        for child in rows:
            if child.kind not in allowed:
                self.zero(child)
        return [child for child in rows if child.kind in allowed]

    def payload(self, box):
        return self.raw[box.data:box.end]

    @staticmethod
    def table(payload, width):
        if len(payload) < 8:
            raise MediaError("media_track_invalid")
        count = int.from_bytes(payload[4:8], "big")
        if not 0 < count <= 100000 or len(payload) != 8 + count * width:
            raise MediaError("media_track_invalid")
        return [payload[8 + n * width:8 + (n + 1) * width] for n in range(count)]

    def description(self, stsd):
        data = self.payload(stsd)
        if len(data) < 8 or data[:8] != b"\x00\x00\x00\x00\x00\x00\x00\x01":
            raise MediaError("media_track_invalid")
        entries = boxes(self.raw, stsd.data + 8, stsd.end)
        if len(entries) != 1 or entries[0].kind not in {b"avc1", b"hvc1", b"hev1"}:
            raise MediaError("media_codec_unsupported")
        entry = entries[0]
        value = self.payload(entry)
        if len(value) < 78 or value[6:8] != b"\x00\x01":
            raise MediaError("media_track_invalid")
        width, height = struct.unpack_from(">HH", value, 24)
        if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 20_000_000:
            raise MediaError("media_track_invalid")
        self.clean[entry.data + 42:entry.data + 74] = bytes(32)
        children = boxes(self.raw, entry.data + 78, entry.end)
        for child in children:
            if child.kind not in {b"avcC", b"hvcC", b"pasp", b"btrt", b"colr"}:
                self.zero(child)
            elif child.kind == b"pasp" and child.end - child.data != 8:
                raise MediaError("media_track_invalid")
            elif child.kind == b"btrt" and child.end - child.data != 12:
                raise MediaError("media_track_invalid")
            elif child.kind == b"colr":
                color = self.payload(child)
                if len(color) not in {10, 11} or color[:4] not in {b"nclx", b"nclc"}:
                    self.zero(child)
        h264 = entry.kind == b"avc1"
        config_box = only(children, b"avcC" if h264 else b"hvcC")
        config = self.payload(config_box)
        if h264:
            if len(config) < 7 or config[0] != 1:
                raise MediaError("media_track_invalid")
            nal_length, cursor = (config[4] & 3) + 1, 6
            sps_count = config[5] & 31
            if sps_count < 1:
                raise MediaError("media_track_invalid")
            for wanted, count in ((7, sps_count), (8, None)):
                if count is None:
                    if cursor >= len(config):
                        raise MediaError("media_track_invalid")
                    count, cursor = config[cursor], cursor + 1
                if count < 1:
                    raise MediaError("media_track_invalid")
                for _ in range(count):
                    if cursor + 2 > len(config):
                        raise MediaError("media_track_invalid")
                    size = int.from_bytes(config[cursor:cursor + 2], "big")
                    cursor += 2
                    if size < 2 or cursor + size > len(config) or config[cursor] & 31 != wanted:
                        raise MediaError("media_track_invalid")
                    cursor += size
            if cursor < len(config):
                # High-profile AVC extension has numeric fields and optional SPS extensions.
                if config[1] not in {100, 110, 122, 144} or len(config) < cursor + 4:
                    raise MediaError("media_track_invalid")
                count, cursor = config[cursor + 3], cursor + 4
                for _ in range(count):
                    if cursor + 2 > len(config):
                        raise MediaError("media_track_invalid")
                    size = int.from_bytes(config[cursor:cursor + 2], "big")
                    cursor += 2
                    if size < 2 or cursor + size > len(config) or config[cursor] & 31 != 13:
                        raise MediaError("media_track_invalid")
                    cursor += size
            if cursor != len(config):
                raise MediaError("media_track_invalid")
        else:
            if len(config) < 23 or config[0] != 1:
                raise MediaError("media_track_invalid")
            nal_length, cursor, found, arrays = (config[21] & 3) + 1, 23, set(), []
            for _ in range(config[22]):
                if cursor + 3 > len(config):
                    raise MediaError("media_track_invalid")
                start = cursor
                kind, count = config[cursor] & 63, int.from_bytes(config[cursor + 1:cursor + 3], "big")
                cursor += 3
                if kind not in {32, 33, 34, 39, 40}:
                    raise MediaError("media_codec_unsupported")
                if count < 1:
                    raise MediaError("media_track_invalid")
                for _ in range(count):
                    if cursor + 2 > len(config):
                        raise MediaError("media_track_invalid")
                    size = int.from_bytes(config[cursor:cursor + 2], "big")
                    cursor += 2
                    if size < 3 or cursor + size > len(config) or config[cursor] >> 1 & 63 != kind:
                        raise MediaError("media_track_invalid")
                    cursor += size
                if kind in {32, 33, 34}:
                    found.add(kind)
                    arrays.append(config[start:cursor])
            if cursor != len(config) or found != {32, 33, 34}:
                raise MediaError("media_track_invalid")
            if len(arrays) != config[22]:
                # HEVC configuration may carry SEI arrays: remove them without moving samples.
                cleaned = config[:22] + bytes([len(arrays)]) + b"".join(arrays)
                header = config_box.data - config_box.start
                size = header + len(cleaned)
                tail = config_box.start + size
                padding = config_box.end - tail
                if padding < 8:
                    raise MediaError("media_track_invalid")
                self.clean[config_box.start:config_box.data] = (struct.pack(">I4s", size, b"hvcC") if header == 8
                                                               else struct.pack(">I4sQ", 1, b"hvcC", size))
                self.clean[config_box.data:tail] = cleaned
                self.clean[tail:config_box.end] = struct.pack(">I4s", padding, b"free") + bytes(padding - 8)
        if nal_length not in {1, 2, 4}:
            raise MediaError("media_track_invalid")
        return h264, nal_length

    def track(self, trak):
        rows = self.children(trak, {b"tkhd", b"mdia", b"edts"})
        tkhd = self.payload(only(rows, b"tkhd"))
        if not tkhd or len(tkhd) != (96 if tkhd[0] == 1 else 84) or tkhd[0] not in {0, 1}:
            raise MediaError("media_track_invalid")
        for edit in [box for box in rows if box.kind == b"edts"]:
            entries = self.children(edit, {b"elst"})
            value = self.payload(only(entries, b"elst"))
            self.table(value, 20 if value and value[0] == 1 else 12)
        mdia = self.children(only(rows, b"mdia"), {b"mdhd", b"hdlr", b"minf"})
        header = self.payload(only(mdia, b"mdhd"))
        if not header or header[0] not in {0, 1} or len(header) != (36 if header[0] == 1 else 24):
            raise MediaError("media_track_invalid")
        timescale = int.from_bytes(header[20:24] if header[0] else header[12:16], "big")
        duration = int.from_bytes(header[24:32] if header[0] else header[16:20], "big")
        if timescale < 1 or duration < 1:
            raise MediaError("media_track_invalid")
        handler = only(mdia, b"hdlr")
        if handler.end - handler.data < 24 or self.raw[handler.data + 8:handler.data + 12] != b"vide":
            raise MediaError("media_codec_unsupported")
        self.clean[handler.data + 24:handler.end] = bytes(handler.end - handler.data - 24)
        minf = self.children(only(mdia, b"minf"), {b"vmhd", b"dinf", b"stbl"})
        if only(minf, b"vmhd").end - only(minf, b"vmhd").data != 12:
            raise MediaError("media_track_invalid")
        refs = self.children(only(minf, b"dinf"), {b"dref"})
        dref = only(refs, b"dref")
        if self.payload(dref)[:8] != b"\x00\x00\x00\x00\x00\x00\x00\x01":
            raise MediaError("media_external_reference_rejected")
        link = only(boxes(self.raw, dref.data + 8, dref.end), b"url ")
        if self.payload(link) != b"\x00\x00\x00\x01":
            raise MediaError("media_external_reference_rejected")
        table = self.children(only(minf, b"stbl"), {b"stsd", b"stts", b"stss", b"ctts", b"stsc", b"stsz", b"stco", b"co64"})
        h264, nal_length = self.description(only(table, b"stsd"))
        stsz = self.payload(only(table, b"stsz"))
        if len(stsz) < 12:
            raise MediaError("media_track_invalid")
        fixed, sample_count = struct.unpack_from(">II", stsz, 4)
        if not 0 < sample_count <= 100000 or len(stsz) != 12 + (0 if fixed else sample_count * 4):
            raise MediaError("media_track_invalid")
        sizes = [fixed] * sample_count if fixed else [int.from_bytes(stsz[12 + n * 4:16 + n * 4], "big") for n in range(sample_count)]
        if any(size < nal_length + 2 for size in sizes):
            raise MediaError("media_track_invalid")
        timings = self.table(self.payload(only(table, b"stts")), 8)
        counts = [struct.unpack(">II", row) for row in timings]
        if any(count < 1 or delta < 1 for count, delta in counts) or sum(count for count, _ in counts) != sample_count:
            raise MediaError("media_track_invalid")
        if sum(count * delta for count, delta in counts) != duration:
            raise MediaError("media_track_invalid")
        for sync in [box for box in table if box.kind == b"stss"]:
            indices = [int.from_bytes(row, "big") for row in self.table(self.payload(sync), 4)]
            if indices[0] != 1 or indices != sorted(set(indices)) or indices[-1] > sample_count:
                raise MediaError("media_track_invalid")
        for ctts in [box for box in table if box.kind == b"ctts"]:
            offsets = self.table(self.payload(ctts), 8)
            if sum(int.from_bytes(row[:4], "big") for row in offsets) != sample_count:
                raise MediaError("media_track_invalid")
        chunk_box = [box for box in table if box.kind in {b"stco", b"co64"}]
        if len(chunk_box) != 1:
            raise MediaError("media_track_invalid")
        offsets = [int.from_bytes(row, "big") for row in self.table(self.payload(chunk_box[0]), 8 if chunk_box[0].kind == b"co64" else 4)]
        mapping = [struct.unpack(">III", row) for row in self.table(self.payload(only(table, b"stsc")), 12)]
        if mapping[0][0] != 1 or any(count < 1 or description != 1 for _, count, description in mapping):
            raise MediaError("media_track_invalid")
        if [row[0] for row in mapping] != sorted(set(row[0] for row in mapping)) or mapping[-1][0] > len(offsets):
            raise MediaError("media_track_invalid")
        sample, group = 0, 0
        for chunk, offset in enumerate(offsets, 1):
            while group + 1 < len(mapping) and mapping[group + 1][0] <= chunk:
                group += 1
            for _ in range(mapping[group][1]):
                if sample >= len(sizes):
                    raise MediaError("media_track_invalid")
                end = offset + sizes[sample]
                if not any(start <= offset and end <= stop for start, stop in self.ranges):
                    raise MediaError("media_track_invalid")
                self.samples.append((offset, end))
                cursor, vcl = offset, False
                while cursor < end:
                    if cursor + nal_length > end:
                        raise MediaError("media_track_invalid")
                    size = int.from_bytes(self.raw[cursor:cursor + nal_length], "big")
                    cursor += nal_length
                    if size < (2 if h264 else 3) or cursor + size > end or self.raw[cursor] & 128:
                        raise MediaError("media_track_invalid")
                    kind = self.raw[cursor] & 31 if h264 else self.raw[cursor] >> 1 & 63
                    if (h264 and kind in {1, 2, 3, 4, 5}) or (not h264 and kind <= 31):
                        vcl = True
                    elif (h264 and kind not in {7, 8}) or (not h264 and kind not in {32, 33, 34}):
                        # Remove all non-picture metadata, including SEI and encoder text.
                        header = b"\x0c" if h264 else b"\x4c\x01"
                        if size < len(header) + 1:
                            raise MediaError("media_track_invalid")
                        self.clean[cursor:cursor + size] = header + b"\xff" * (size - len(header) - 1) + b"\x80"
                    cursor += size
                if not vcl:
                    raise MediaError("media_track_invalid")
                offset, sample = end, sample + 1
        if sample != sample_count:
            raise MediaError("media_track_invalid")

    def validate(self, *, secrets=(), prompt_digest=None, prompt_length=None):
        root = boxes(self.raw, 0, len(self.raw))
        ftyp = self.payload(only(root, b"ftyp"))
        brands = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"hvc1", b"hev1", b"qt  "}
        if len(ftyp) < 8 or (len(ftyp) - 8) % 4 or ftyp[:4] not in brands or any(ftyp[n:n + 4] not in brands for n in range(8, len(ftyp), 4)):
            raise MediaError("media_track_invalid")
        self.ranges = [(box.data, box.end) for box in root if box.kind == b"mdat" and box.end > box.data]
        if not self.ranges:
            raise MediaError("media_track_invalid")
        for box in root:
            if box.kind not in {b"ftyp", b"moov", b"mdat"}:
                self.zero(box)
        movie_box = only(root, b"moov")
        if movie_box.end - movie_box.start > 256 * 1024:
            raise MediaError("media_track_invalid")
        moov = self.children(movie_box, {b"mvhd", b"trak"})
        movie = self.payload(only(moov, b"mvhd"))
        if not movie or movie[0] not in {0, 1} or len(movie) != (112 if movie[0] else 100):
            raise MediaError("media_track_invalid")
        tracks = [box for box in moov if box.kind == b"trak"]
        if len(tracks) != 1:
            raise MediaError("media_codec_unsupported")
        self.track(tracks[0])
        # Unreferenced mdat padding may contain arbitrary metadata: clear every gap.
        ordered = sorted(self.samples)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise MediaError("media_track_invalid")
        for start, end in self.ranges:
            cursor = start
            for first, last in ordered:
                if start <= first and last <= end:
                    self.clean[cursor:first] = bytes(first - cursor)
                    cursor = last
            self.clean[cursor:end] = bytes(end - cursor)
        cleaned = bytes(self.clean)
        try:
            if media_contains_caller_text(cleaned, secrets=secrets, prompt_digest=prompt_digest, prompt_length=prompt_length):
                raise MediaError("sensitive_media_metadata_rejected")
        except PrivacyScanLimit:
            raise MediaError("media_privacy_check_inconclusive") from None
        return cleaned


def sanitize_mp4(content, *, secrets=(), prompt_digest=None, prompt_length=None):
    try:
        return MP4(content).validate(secrets=secrets, prompt_digest=prompt_digest, prompt_length=prompt_length)
    except MediaError:
        raise
    except (ValueError, IndexError, struct.error, OverflowError):
        raise MediaError("media_track_invalid") from None


def valid_mp4(content):
    try:
        sanitize_mp4(content)
        return True
    except MediaError:
        return False
