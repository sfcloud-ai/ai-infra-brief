"""处理有大小限制的 Slack 托管 PNG/JPEG，不使用磁盘缓存或图片解码。"""
import re
import struct
import urllib.parse
import zlib

MAX_IMAGE_BYTES = 2_000_000  # 采用保守上限，不超过 2 MB 的任一种计量口径。
MAX_IMAGES = 10
MAX_PIXELS = 40_000_000
MIMES = {"image/png": "png", "image/jpeg": "jpg"}


class ImageError(Exception):
    """此边界仅允许传递固定且不含敏感信息的错误码。"""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def image_url(url, team_id, file_id):
    if not isinstance(url, str) or any(char.isspace() for char in url):
        raise ImageError("image_url_rejected")
    try:
        parsed = urllib.parse.urlsplit(url)
        decoded = urllib.parse.unquote(parsed.path)
        prefix = "/files-pri/" + team_id + "-" + file_id + "/"
        if (parsed.scheme != "https" or parsed.netloc != "files.slack.com"
                or parsed.fragment or not decoded.startswith(prefix)
                or "\\" in decoded or "%" in decoded
                or any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
            raise ImageError("image_url_rejected")
        suffix = decoded[len(prefix):].split("/")
        if (any(part in ("", ".", "..") for part in suffix)
                or not (len(suffix) == 1 or (len(suffix) == 2 and suffix[0] == "download"))):
            raise ImageError("image_url_rejected")
    except ValueError:
        raise ImageError("image_url_rejected") from None
    return url


def validate_file(item, team_id):
    if not isinstance(item, dict):
        raise ImageError("invalid_image_metadata")
    file_id = item.get("id")
    if not isinstance(file_id, str) or not re.fullmatch(r"F[A-Z0-9]{1,64}", file_id):
        raise ImageError("invalid_image_id")
    mime = item.get("mimetype")
    if mime not in MIMES:
        raise ImageError("unsupported_image_format")
    if item.get("filetype") not in ({"png"} if mime == "image/png" else {"jpg", "jpeg"}):
        raise ImageError("image_type_mismatch")
    if (item.get("mode") != "hosted" or item.get("is_external") is not False
            or item.get("file_access") != "visible"
            or item.get("user_team", team_id) != team_id):
        raise ImageError("image_not_slack_hosted")
    size = item.get("size")
    if type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES:
        raise ImageError("image_size_rejected")
    url = image_url(item.get("url_private") or item.get("url_private_download"), team_id, file_id)
    return {"file_id": file_id, "mime": mime, "size": size, "url": url}


def select_images(message, team_id):
    files = message.get("files", [])
    if not isinstance(files, list):
        return [{"file_id": "invalid-list", "error": "invalid_image_metadata"}]
    candidates = [item for item in files if isinstance(item, dict)
                  and isinstance(item.get("mimetype"), str)
                  and item["mimetype"].startswith("image/")]
    if len(candidates) > MAX_IMAGES:
        return [{"file_id": "attachment-limit", "error": "too_many_images"}]
    plans, seen = [], set()
    for index, item in enumerate(candidates):
        try:
            plan = validate_file(item, team_id)
        except ImageError as error:
            file_id = item.get("id")
            if not isinstance(file_id, str) or not re.fullmatch(r"F[A-Z0-9]{1,64}", file_id):
                file_id = "invalid-" + str(index)
            plan = {"file_id": file_id, "error": error.code}
        if plan["file_id"] not in seen:
            seen.add(plan["file_id"])
            plans.append(plan)
    return plans


def _dimensions(width, height):
    if not 0 < width <= 16000 or not 0 < height <= 16000 or width * height > MAX_PIXELS:
        raise ImageError("image_dimensions_rejected")
    return width, height


def _png(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageError("image_format_invalid")
    offset, count, dimensions, saw_data = 8, 0, None, False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data) or count >= 4096:
            raise ImageError("image_format_invalid")
        chunk = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + chunk) & 0xffffffff != crc:
            raise ImageError("image_format_invalid")
        if count == 0:
            if kind != b"IHDR" or length != 13:
                raise ImageError("image_format_invalid")
            dimensions = _dimensions(*struct.unpack_from(">II", chunk))
            depth, color, compression, filtering, interlace = chunk[8:13]
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise ImageError("image_format_invalid")
        elif kind == b"IHDR" or kind == b"acTL":
            raise ImageError("image_format_invalid")
        if kind == b"IDAT":
            saw_data = True
        if kind == b"IEND":
            if length != 0 or end != len(data) or not saw_data or dimensions is None:
                raise ImageError("image_format_invalid")
            return dimensions
        offset, count = end, count + 1
    raise ImageError("image_format_invalid")


def _jpeg(data):
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ImageError("image_format_invalid")
    offset, dimensions = 2, None
    while offset + 4 <= len(data):
        if data[offset] != 0xff:
            raise ImageError("image_format_invalid")
        while offset < len(data) and data[offset] == 0xff:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0, 0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            raise ImageError("image_format_invalid")
        length = struct.unpack_from(">H", data, offset)[0]
        if length < 2 or offset + length > len(data) - 2:
            raise ImageError("image_format_invalid")
        segment = data[offset + 2:offset + length]
        if marker in (0xc0, 0xc1, 0xc2):
            if len(segment) < 6 or segment[0] not in (8, 12):
                raise ImageError("image_format_invalid")
            height, width = struct.unpack_from(">HH", segment, 1)
            dimensions = _dimensions(width, height)
            if len(segment) != 6 + 3 * segment[5]:
                raise ImageError("image_format_invalid")
        if marker == 0xda:
            if dimensions is None or len(segment) < 6:
                raise ImageError("image_format_invalid")
            return dimensions
        offset += length
    raise ImageError("image_format_invalid")


def validate_image(data, mime):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ImageError("image_size_rejected")
    # 校验容器和文件头的完整性，不解压不可信的像素数据。
    try:
        if mime == "image/png":
            return _png(data)
        if mime == "image/jpeg":
            return _jpeg(data)
    except (struct.error, IndexError, OverflowError):
        raise ImageError("image_format_invalid") from None
    raise ImageError("unsupported_image_format")
