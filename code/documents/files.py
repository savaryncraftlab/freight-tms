# From apps/documents/files.py in the private repository.
# Shown here as a sample: the rest of the system is in the private repo.

"""Checks every uploaded file before it is stored (security rules in the project spec).

- The type is detected from the content (puremagic), never taken from the
  file name or from what the browser or Telegram says.
- Only PDF, JPEG and PNG are accepted. HEIC photos (iPhone) are refused for now.
- Images are opened with Pillow, checked and saved again from their pixels
  alone: EXIF with the GPS position of the phone, XMP and comments are dropped
  (GPS is personal data under RODO). A huge image that would fill the memory
  (a "decompression bomb") is refused before it is decoded.
- PDFs are stored as they are. A coarse check refuses those whose readable
  parts name scripts, launch actions or attached files; it does not open
  compressed parts, so it is no guarantee. The main protection is elsewhere:
  the file is served as an attachment with "nosniff" (documents/views.py), and
  the previews in our pages draw it with pdf.js with annotations off, so no
  script of the PDF runs (static/js/document-preview.js).
- A PDF from the drivers' bot is also refused when it still has the phone's
  GPS position, in a photo's EXIF or in XMP (check_pdf_photos()): a phone's
  "save as PDF" keeps the photo's metadata, and the PDF is stored as it is.
"""

import io
import re
import zlib
from typing import NamedTuple

import puremagic
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from PIL import ExifTags, Image, ImageOps

# Detected type -> extension of the stored file.
EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}

# Detected image type -> the only Pillow decoder allowed to open it.
PILLOW_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
}

HEIC_TYPES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}

# A coarse filter, no more. Stream contents (compressed scan images) are skipped
# first: by chance they contain bytes like "/JS" and would refuse ordinary
# scans. Names written with #hex escapes are decoded (/J#61vaScript is
# /JavaScript). Then only whole PDF names count: a name ends at whitespace or a
# delimiter. Dictionaries inside compressed object streams, and a dictionary
# hidden between fake "stream" markers, still pass: we do not unpack PDFs. The
# real protection is that a PDF is served as an attachment with "nosniff" and
# its previews run no PDF script (pdf.js, annotations off).
PDF_REFUSED_NAME = re.compile(
    rb"/(?:JavaScript|JS|Launch|EmbeddedFiles?)(?=[\s\x00/<>\[\](){}%]|$)"
)
# In a PDF name "#" and two hex digits stand for one character. A fixed-length
# pattern: replacing all of them takes one pass over the bytes.
PDF_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")

# A scan has a few streams per page (the image, the page's drawing); an office
# PDF some hundreds. Far more is not a document but an attempt to keep the
# server busy: refused.
MAX_PDF_STREAMS = 10_000

# 16-bit greyscale (scanners): one pixel value is 0..65535.
SIXTEEN_BIT_MODES = {"I", "I;16", "I;16B", "I;16L", "I;16N"}

JPEG_QUALITY = 85

# How a JPEG starts (SOI): a photo inside a PDF, its bytes as they are (DCTDecode).
JPEG_START = b"\xff\xd8"
# The EXIF tag (0x8825) that points to the GPS block: where a phone writes its position.
EXIF_GPS = ExifTags.IFD.GPSInfo
# The position itself in that block: the latitude (2) and the longitude (4). A
# block with only its version (GPSVersionID, 0) or the time holds no place.
GPS_COORDINATES = {ExifTags.GPS.GPSLatitude, ExifTags.GPS.GPSLongitude}
# The same position in XMP, the text metadata a phone may write instead of (or
# besides) EXIF: in the photo, or in the PDF's own metadata.
XMP_GPS = re.compile(rb"exif:GPS(?:Latitude|Longitude)")
# How an XMP packet starts: the PDF's metadata, a stream of its own. With the
# packet wrapper, or without it: an XML declaration, the old Adobe wrapper, or
# the RDF itself (review R1 of 03.11.2026).
XMP_START = (b"<?xpacket", b"<x:xmpmeta", b"<x:xapmeta", b"<?xml", b"<rdf:RDF")
# What may come before it: white space, and a UTF-8 byte order mark.
XMP_LEADING = b" \t\r\n\xef\xbb\xbf"
# A compressed stream (FlateDecode) is opened only to look at its first bytes:
# a photo behind /FlateDecode /DCTDecode, or an XMP packet. Only those are
# inflated whole, together at most this many times the upload limit per PDF.
# A real scan's photos inflate to about their own size (a JPEG does not
# compress further); far more is a "zip bomb" meant to fill the memory:
# refused as too complex.
FLATE_PEEK = 64
INFLATED_PER_UPLOAD_LIMIT = 2


class CleanFile(NamedTuple):
    content: bytes
    content_type: str
    extension: str


def clean_file(uploaded_file):
    """Check an uploaded file and return what may be stored, or raise ValidationError."""
    # This only protects the checks below: Django has already received the
    # whole request by now. The real cap on the request body belongs in the
    # reverse proxy in front of Django.
    limit = settings.DOCUMENT_MAX_UPLOAD_SIZE
    too_big = ValidationError(
        _("The file is larger than %(limit)s MB.") % {"limit": limit // (1024 * 1024)},
        code="too_big",
    )
    if uploaded_file.size is not None and uploaded_file.size > limit:
        raise too_big
    uploaded_file.seek(0)
    data = uploaded_file.read(limit + 1)
    if len(data) > limit:
        raise too_big
    if not data:
        raise ValidationError(_("The file is empty."), code="empty")

    content_type = detect_type(data)
    if content_type == "application/pdf":
        content = _clean_pdf(data)
    else:
        content = _clean_image(data, PILLOW_FORMATS[content_type])
    return CleanFile(content, content_type, EXTENSIONS[content_type])


def detect_type(data):
    """The accepted type of these bytes, or ValidationError. The file name plays no part."""
    detected = [match.mime_type for match in puremagic.magic_string(data)]
    for content_type in detected:
        if content_type in EXTENSIONS:
            return content_type
    if HEIC_TYPES.intersection(detected):
        raise ValidationError(
            _("HEIC photos are not accepted yet: send the photo as JPEG."), code="heic"
        )
    raise ValidationError(
        _("This type of file is not accepted: send a PDF, JPEG or PNG."), code="file_type"
    )


def _clean_pdf(data):
    damaged = ValidationError(_("This is not a valid PDF file."), code="damaged")
    if not data.startswith(b"%PDF-"):
        raise damaged
    readable = _decode_name_escapes(_outside_streams(data, damaged))
    if PDF_REFUSED_NAME.search(readable):
        raise ValidationError(
            _("PDFs with scripts, actions or attached files are not accepted."),
            code="pdf_active_content",
        )
    return data


def _decode_name_escapes(data):
    """ "/J#61vaScript" -> "/JavaScript", so an escaped name is checked like a plain one."""
    return PDF_NAME_ESCAPE.sub(lambda match: bytes([int(match.group(1), 16)]), data)


def _streams(data, damaged):
    """(start, body, end) of every stream, found in one pass over the bytes:
    where the keyword "stream" is, where its data begins, where "endstream" is.

    A stream is the keyword "stream", a line break, the data, and "endstream".
    The line break is CR LF or LF; a lone CR breaks the rule, but PDF readers
    take it all the same, so it counts here too (otherwise the data of such a
    stream would be neither skipped nor checked as a stream).
    Each search (bytes.find) starts where the previous one stopped, so every
    byte is looked at a fixed number of times. (A regular expression did this
    before and slowed down with the square of the size on a file full of
    "stream" without an "endstream": 400 KB took two minutes of the server.)
    A stream that is never closed makes the file damaged.
    """
    position, streams = 0, 0
    while (start := data.find(b"stream", position)) != -1:
        body = start + len(b"stream")
        if data.startswith(b"\r\n", body):
            body += 2
        elif data.startswith((b"\n", b"\r"), body):
            body += 1
        else:  # the word alone, not a stream: look further
            position = body
            continue
        end = data.find(b"endstream", body)
        if end == -1:
            raise damaged
        streams += 1
        if streams > MAX_PDF_STREAMS:
            raise ValidationError(
                _("This PDF has too many parts to be checked: send a simpler scan."),
                code="pdf_too_complex",
            )
        yield start, body, end
        position = end + len(b"endstream")


def _outside_streams(data, damaged):
    """The PDF without the contents of its streams (a lone word "stream" stays)."""
    kept, position = [], 0
    for start, _body, end in _streams(data, damaged):
        kept.append(data[position:start])
        position = end + len(b"endstream")
    kept.append(data[position:])
    return b"".join(kept)


def check_pdf_photos(data):
    """A PDF from the drivers' bot: refused if it still has the phone's GPS
    position (RODO: no GPS from drivers' pictures is stored).

    A phone's "save as PDF" puts the photo's JPEG bytes into the PDF as they
    are, EXIF and all, and clean_file() stores a PDF unchanged. Looked for:
    - in a photo's EXIF: every stream that starts like a JPEG is opened with
      Pillow, which reads its header only (no pixel is decoded), under the
      pixel limit of a photo (_jpeg_has_gps());
    - the same behind /FlateDecode /DCTDecode: a compressed stream that
      starts like a JPEG once inflated (_inflated()) is opened the same way;
    - in XMP (XMP_GPS): in the PDF's metadata or a photo's, as it is or
      inflated.
    Only a position counts: a GPS block with its version alone is no place.
    The office's uploads are not checked here: the rule is about the
    drivers' pictures.
    """
    damaged = ValidationError(_("This is not a valid PDF file."), code="damaged")
    if XMP_GPS.search(data):  # XMP as it is, wherever it is
        raise _location_refused()
    left = INFLATED_PER_UPLOAD_LIMIT * settings.DOCUMENT_MAX_UPLOAD_SIZE
    for _start, body, end in _streams(data, damaged):
        content = data[body:end]
        if not content.startswith(JPEG_START):
            content, left = _inflated(content, left, damaged)
            if XMP_GPS.search(content):
                raise _location_refused()
        if content.startswith(JPEG_START) and _jpeg_has_gps(content, damaged):
            raise _location_refused()


def _location_refused():
    return ValidationError(
        _(
            "This PDF has the phone's location in it: scan the CMR with a scanner app "
            "and send that PDF."
        ),
        code="pdf_location",
    )


def _inflated(stream, left, damaged):
    """(the stream inflated, the inflated bytes still allowed) for a compressed
    stream (FlateDecode) that holds a photo or an XMP packet; (b"", left) for
    any other stream, of which only the first bytes are inflated (FLATE_PEEK).

    At most `left` bytes come out: more refuses the PDF as too complex (a
    "zip bomb"). A stream that breaks off while it is inflated cannot be
    checked: the PDF is damaged.
    """
    try:
        start = zlib.decompressobj().decompress(stream, FLATE_PEEK)
    except zlib.error:
        return b"", left  # not compressed, or not with Flate
    if not (start.startswith(JPEG_START) or start.lstrip(XMP_LEADING).startswith(XMP_START)):
        return b"", left
    try:
        content = zlib.decompressobj().decompress(stream, left + 1)
    except zlib.error as error:
        raise damaged from error
    if len(content) > left:
        raise ValidationError(
            _("This PDF has too many parts to be checked: send a simpler scan."),
            code="pdf_too_complex",
        )
    return content, left - len(content)


def _jpeg_has_gps(jpeg, damaged):
    """Whether the EXIF of this JPEG has a GPS position (GPS_COORDINATES). Raises
    ValidationError when the photo is too big or cannot be read (then its EXIF
    cannot be checked)."""
    too_large = ValidationError(_("The image has too many pixels."), code="too_many_pixels")
    try:
        with Image.open(io.BytesIO(jpeg), formats=["JPEG"]) as image:
            if image.width * image.height > settings.DOCUMENT_MAX_PIXELS:
                raise too_large
            gps = image.getexif().get_ifd(EXIF_GPS)
            return not GPS_COORDINATES.isdisjoint(gps)
    except ValidationError:
        raise
    except Image.DecompressionBombError as error:  # Pillow's own, much higher limit
        raise too_large from error
    except Exception as error:  # Pillow raises many kinds of errors for broken files
        raise damaged from error


def _clean_image(data, pillow_format):
    """Decode the image and save it again from its pixels only."""
    damaged = ValidationError(_("The image is damaged or not a real JPEG or PNG."), code="damaged")
    too_large = ValidationError(_("The image has too many pixels."), code="too_many_pixels")
    try:
        # Image.open() reads only the header, so the size is checked before any
        # pixel is decoded. A decoded pixel takes 3-4 bytes, and cleaning holds
        # about three copies: the limit keeps one upload to a few hundred MB.
        with Image.open(io.BytesIO(data), formats=[pillow_format]) as image:
            if image.width * image.height > settings.DOCUMENT_MAX_PIXELS:
                raise too_large
            image.verify()  # checks the structure; the image cannot be used afterwards
        with Image.open(io.BytesIO(data), formats=[pillow_format]) as image:
            image.load()
            pixels = _pixels_only(_upright(image), pillow_format)
    except ValidationError:
        raise
    except Image.DecompressionBombError as error:  # Pillow's own, much higher limit
        raise too_large from error
    except Exception as error:  # Pillow raises many kinds of errors for broken files
        raise damaged from error

    output = io.BytesIO()
    if pillow_format == "JPEG":
        pixels.save(output, "JPEG", quality=JPEG_QUALITY, optimize=True)
    else:
        pixels.save(output, "PNG", optimize=True)
    return output.getvalue()


def _upright(image):
    """Turn a phone photo the way the phone showed it.

    The phone stores "rotate by 90°" in EXIF; once EXIF is dropped, the photo
    would lie on its side.
    """
    try:
        return ImageOps.exif_transpose(image)
    except Exception:  # a broken EXIF block must not refuse a good photo
        return image


def _pixels_only(image, pillow_format):
    """A new image built from the pixels alone.

    Nothing from image.info (EXIF with GPS, XMP, comments, text chunks) is
    copied: the new image does not know where the pixels came from.
    """
    if image.mode in SIXTEEN_BIT_MODES:
        # Scaled down to 0..255 first: a plain convert() would cut every value
        # above 255 to white, and a scan would come out as a blank page.
        image = image.convert("I").point(lambda value: value * (1 / 256)).convert("L")
    if pillow_format == "JPEG":
        if image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
    elif image.mode not in ("1", "L", "LA", "RGB", "RGBA"):
        # A palette (P) would be lost by frombytes(), so it becomes plain colours.
        image = image.convert("RGBA" if image.has_transparency_data else "RGB")
    clean = Image.new(image.mode, image.size)
    clean.paste(image)  # pixels only, without an extra copy as bytes
    return clean
