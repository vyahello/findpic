"""Rules that answer "what does this file give away about me?".

Each finding carries a remediation string: the exact exiftool command that
removes that specific leak. Every one of them writes to a copy — findpic never
suggests a command that destroys the user's original. Commands are not
translated; they have to run exactly as printed.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import Category, Confidence, Finding, Severity
from ..util import truncate
from .context import Context
from .registry import rule

#: ``(tag, label key)``. Labels live in the catalogue so they translate.
IDENTITY_TAGS: tuple[tuple[str, str], ...] = (
    ("IFD0:Artist", "artist"),
    ("IFD0:Copyright", "copyright"),
    ("ExifIFD:OwnerName", "camera_owner"),
    ("ExifIFD:CameraOwnerName", "camera_owner"),
    ("XMP-dc:Creator", "creator"),
    ("XMP-dc:Rights", "rights"),
    ("IPTC:By-line", "iptc_byline"),
    ("IPTC:By-lineTitle", "iptc_byline_title"),
    ("IPTC:Credit", "iptc_credit"),
    ("IPTC:Source", "iptc_source"),
    ("IPTC:Contact", "iptc_contact"),
    ("XMP-iptcCore:CreatorWorkEmail", "creator_email"),
    ("XMP-iptcCore:CreatorWorkTelephone", "creator_phone"),
    ("XMP-iptcCore:CreatorWorkURL", "creator_url"),
    ("XMP-photoshop:AuthorsPosition", "author_position"),
)

#: Identifiers that link several photos back to one physical device.
DEVICE_ID_TAGS: tuple[tuple[str, str], ...] = (
    ("ExifIFD:SerialNumber", "body_serial"),
    ("ExifIFD:BodySerialNumber", "body_serial"),
    ("ExifIFD:LensSerialNumber", "lens_serial"),
    ("ExifIFD:ImageUniqueID", "image_unique_id"),
    ("Apple:ContentIdentifier", "apple_content_id"),
    ("Apple:PhotoIdentifier", "apple_photo_id"),
    ("Apple:ImageCaptureRequestID", "apple_request_id"),
    ("XMP-xmpMM:DocumentID", "document_id"),
    ("XMP-xmpMM:InstanceID", "instance_id"),
    ("XMP-xmpMM:OriginalDocumentID", "original_document_id"),
    ("MakerNotes:InternalSerialNumber", "internal_serial"),
)

#: Free-text fields whose contents the owner may not realise are travelling.
TEXT_TAGS: tuple[tuple[str, str], ...] = (
    ("ExifIFD:UserComment", "user_comment"),
    ("IFD0:ImageDescription", "image_description"),
    ("XMP-dc:Description", "description"),
    ("XMP-dc:Title", "title"),
    ("IPTC:Caption-Abstract", "iptc_caption"),
    ("IPTC:Headline", "iptc_headline"),
    ("IPTC:ObjectName", "iptc_object_name"),
    ("IPTC:SpecialInstructions", "iptc_instructions"),
    ("File:Comment", "jpeg_comment"),
)

#: Where the picture was taken, written as words rather than coordinates.
PLACE_NAME_TAGS: tuple[tuple[str, str], ...] = (
    ("XMP-iptcExt:LocationCreatedCity", "city"),
    ("XMP-iptcExt:LocationCreatedSublocation", "sublocation"),
    ("XMP-iptcExt:LocationCreatedProvinceState", "state"),
    ("XMP-iptcExt:LocationCreatedCountryName", "country"),
    ("IPTC:City", "city"),
    ("IPTC:Sub-location", "sublocation"),
    ("IPTC:Province-State", "state"),
    ("IPTC:Country-PrimaryLocationName", "country"),
)


def _present(context: Context, tags: tuple[tuple[str, str], ...]) -> list[tuple[str, str, str]]:
    """Return ``(tag, label_key, value)`` for each tag carrying a real value."""
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for tag, label_key in tags:
        value = context.meta.str(tag)
        if not value:
            continue
        key = f"{label_key}:{value}"
        if key in seen:
            continue
        seen.add(key)
        found.append((tag, label_key, value))
    return found


@rule("gps_location", Category.PRIVACY, order=1)
def gps_location(context: Context) -> Iterable[Finding]:
    """Coordinates are the single most consequential thing a photo can carry."""
    location = context.location
    if not location.present:
        return

    yield Finding(
        id="privacy.gps_location",
        category=Category.PRIVACY,
        severity=Severity.CRITICAL,
        confidence=Confidence.HIGH,
        params={
            "where": location.place or location.decimal,
            "coords": location.decimal,
            # An optional trailing sentence. An empty list resolves to an empty
            # string, so the sentence disappears when there is no accuracy value.
            "accuracy_pairs": (
                [("metres", f"{location.accuracy_m:g}")] if location.accuracy_m else []
            ),
        },
        evidence={
            "GPSLatitude": location.latitude,
            "GPSLongitude": location.longitude,
            "GPSHPositioningError": location.accuracy_m,
            "place": location.place,
        },
        weight=40,
        remediation="exiftool -gps:all= -xmp:geotag= -o clean_copy.jpg photo.jpg",
    )

    extras: list[tuple[str, str]] = []
    if location.altitude_m is not None:
        extras.append(("altitude", f"{location.altitude_m:.0f}"))
    if location.direction_deg is not None:
        extras.append(("bearing", f"{location.direction_deg:.0f}"))
    if location.speed:
        extras.append(("speed", f"{location.speed:g} {location.speed_ref or ''}".strip()))
    if not extras:
        return

    yield Finding(
        id="privacy.gps_detail",
        category=Category.PRIVACY,
        severity=Severity.NOTICE,
        confidence=Confidence.HIGH,
        params={"extra_pairs": extras},
        evidence={
            "GPSAltitude": location.altitude_m,
            "GPSImgDirection": location.direction_deg,
            "GPSSpeed": location.speed,
        },
        weight=5,
    )


@rule("place_names", Category.PRIVACY, order=2)
def place_names(context: Context) -> Iterable[Finding]:
    """Written place names survive a GPS strip and are often forgotten."""
    found = _present(context, PLACE_NAME_TAGS)
    if not found:
        return
    values = ", ".join(dict.fromkeys(value for _, _, value in found))
    yield Finding(
        id="privacy.place_names",
        category=Category.PRIVACY,
        severity=Severity.WARNING,
        confidence=Confidence.HIGH,
        params={"values": truncate(values, 80)},
        evidence={tag: value for tag, _, value in found},
        weight=15,
        remediation="exiftool -iptc:all= -xmp-iptcExt:all= -o clean_copy.jpg photo.jpg",
    )


@rule("identity_tags", Category.PRIVACY, order=3)
def identity_tags(context: Context) -> Iterable[Finding]:
    """Names, emails, phone numbers and copyright lines."""
    found = _present(context, IDENTITY_TAGS)
    if not found:
        return
    yield Finding(
        id="privacy.identity_tags",
        category=Category.PRIVACY,
        severity=Severity.WARNING,
        confidence=Confidence.HIGH,
        params={"tag_pairs": [(key, truncate(value, 40)) for _, key, value in found[:3]]},
        evidence={tag: value for tag, _, value in found},
        weight=20,
        remediation=(
            "exiftool -artist= -copyright= -ownername= -xmp:creator= -iptc:all= "
            "-o clean_copy.jpg photo.jpg"
        ),
    )


@rule("named_people", Category.PRIVACY, order=4)
def named_people(context: Context) -> Iterable[Finding]:
    """Actual human names attached to faces — the worst case for a shared photo."""
    named = [person.name for person in context.people if person.name]
    if not named:
        return
    yield Finding(
        id="privacy.named_people",
        category=Category.PRIVACY,
        severity=Severity.CRITICAL,
        confidence=Confidence.HIGH,
        params={"names": ", ".join(named[:5])},
        evidence={"names": named},
        weight=30,
        remediation="exiftool -xmp:all= -o clean_copy.jpg photo.jpg",
    )


@rule("face_regions", Category.PRIVACY, order=5)
def face_regions(context: Context) -> Iterable[Finding]:
    """Unnamed face boxes still reveal how many people are in the frame."""
    anonymous = [p for p in context.people if not p.name]
    if not anonymous:
        return
    yield Finding(
        id="privacy.face_regions",
        category=Category.PRIVACY,
        severity=Severity.NOTICE,
        confidence=Confidence.HIGH,
        count=len(anonymous),
        evidence={"regions": [{"x": p.x, "y": p.y, "w": p.w, "h": p.h} for p in anonymous]},
        weight=10,
        remediation="exiftool -xmp-mwg-rs:all= -o clean_copy.jpg photo.jpg",
    )


@rule("device_identifiers", Category.PRIVACY, order=6)
def device_identifiers(context: Context) -> Iterable[Finding]:
    """Serials and UUIDs that link separate photos to one device."""
    found = _present(context, DEVICE_ID_TAGS)
    if not found:
        return
    label_keys = list(dict.fromkeys(key for _, key, _ in found))
    hard_serial = any("serial" in key for key in label_keys)
    yield Finding(
        id="privacy.device_identifiers",
        category=Category.PRIVACY,
        severity=Severity.WARNING if hard_serial else Severity.NOTICE,
        confidence=Confidence.HIGH,
        count=len(found),
        params={"tag_keys": label_keys[:4]},
        evidence={tag: value for tag, _, value in found},
        weight=15 if hard_serial else 8,
        remediation=(
            "exiftool -serialnumber= -lensserialnumber= -imageuniqueid= "
            "-makernotes:all= -o clean_copy.jpg photo.jpg"
        ),
    )


@rule("free_text", Category.PRIVACY, order=7)
def free_text(context: Context) -> Iterable[Finding]:
    """Captions, comments and titles the owner may have forgotten."""
    found = _present(context, TEXT_TAGS)
    if not found:
        return
    yield Finding(
        id="privacy.free_text",
        category=Category.PRIVACY,
        severity=Severity.NOTICE,
        confidence=Confidence.HIGH,
        count=len(found),
        params={"tag_pairs": [(key, truncate(value, 60)) for _, key, value in found[:4]]},
        evidence={tag: value for tag, _, value in found},
        weight=8,
        remediation=(
            "exiftool -usercomment= -imagedescription= -comment= -o clean_copy.jpg photo.jpg"
        ),
    )


@rule("keywords", Category.PRIVACY, order=8)
def keywords(context: Context) -> Iterable[Finding]:
    """Keyword and subject lists, often auto-generated by photo managers."""
    values: list[str] = []
    for tag in ("IPTC:Keywords", "XMP-dc:Subject", "XMP-lr:HierarchicalSubject"):
        value = context.meta.get(tag)
        if not value:
            continue
        values.extend(str(v) for v in (value if isinstance(value, list) else [value]))
    values = list(dict.fromkeys(v for v in values if v))
    if not values:
        return
    yield Finding(
        id="privacy.keywords",
        category=Category.PRIVACY,
        severity=Severity.NOTICE,
        confidence=Confidence.HIGH,
        count=len(values),
        params={"values": truncate(", ".join(values), 70)},
        evidence={"keywords": values},
        weight=8,
        remediation=(
            "exiftool -keywords= -subject= -hierarchicalsubject= -o clean_copy.jpg photo.jpg"
        ),
    )


@rule("timezone_leak", Category.PRIVACY, order=9)
def timezone_leak(context: Context) -> Iterable[Finding]:
    """The offset tag pins you to a longitude band even with GPS stripped."""
    offset = context.capture.taken_offset
    if not offset:
        return
    yield Finding(
        id="privacy.timezone",
        category=Category.PRIVACY,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        params={"offset": offset},
        evidence={"OffsetTimeOriginal": offset},
        weight=5,
        remediation="exiftool -offsettime*= -o clean_copy.jpg photo.jpg",
    )


@rule("device_uptime", Category.PRIVACY, order=10)
def device_uptime(context: Context) -> Iterable[Finding]:
    """Apple's RunTime is a per-boot clock, usable to cluster a day's photos."""
    uptime = context.device.uptime_seconds
    if not uptime:
        return
    yield Finding(
        id="privacy.device_uptime",
        category=Category.PRIVACY,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        params={"uptime_seconds": uptime},
        evidence={"RunTimeSecondsSincePowerUp": round(uptime, 3)},
        weight=3,
        remediation="exiftool -makernotes:all= -o clean_copy.jpg photo.jpg",
    )


@rule("embedded_thumbnail", Category.PRIVACY, order=11)
def embedded_thumbnail(context: Context) -> Iterable[Finding]:
    """A stale thumbnail can still show what was cropped out of the main image."""
    image = context.image
    if not image.has_thumbnail:
        return

    # The leak only bites when the main image was resized or cropped after the
    # thumbnail was written, so check that here rather than always crying wolf.
    meta = context.meta
    exif_size = (meta.int("ExifIFD:ExifImageWidth"), meta.int("ExifIFD:ExifImageHeight"))
    real_size = (meta.int("File:ImageWidth"), meta.int("File:ImageHeight"))
    resized = (
        all(exif_size)
        and all(real_size)
        and exif_size != real_size
        and exif_size != real_size[::-1]
    )

    if resized:
        yield Finding(
            id="privacy.stale_thumbnail",
            category=Category.PRIVACY,
            severity=Severity.WARNING,
            confidence=Confidence.MEDIUM,
            evidence={
                "ThumbnailLength": image.thumbnail_size,
                "ExifImageSize": exif_size,
                "ActualSize": real_size,
            },
            weight=18,
            remediation="exiftool -thumbnailimage= -o clean_copy.jpg photo.jpg",
        )
        return

    yield Finding(
        id="privacy.embedded_thumbnail",
        category=Category.PRIVACY,
        severity=Severity.INFO,
        confidence=Confidence.HIGH,
        evidence={"ThumbnailLength": image.thumbnail_size},
        weight=2,
    )
