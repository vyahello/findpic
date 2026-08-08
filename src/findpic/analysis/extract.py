"""Turn raw exiftool tags into the structured fields the report is built from.

Extraction is deliberately separate from rule evaluation: extractors answer
"what does this photo say about itself", rules answer "should you believe it".
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from ..exif import Metadata, hash_file
from ..geocode import Geocoder
from ..models import (
    CaptureInfo,
    DeviceInfo,
    FileInfo,
    ImageInfo,
    LocationInfo,
    PersonRegion,
)
from ..tables import (
    APPLE_CAMERA_TYPE,
    APPLE_IMAGE_CAPTURE_TYPE,
    BARE_VERSION,
    apple_os_name,
    match_editor,
)
from ..util import (
    coords_to_dms,
    format_datetime,
    human_bytes,
    parse_exif_datetime,
    same_moment,
)
from .context import AnalysisOptions


def extract_file(path: Path, meta: Metadata, options: AnalysisOptions) -> FileInfo:
    try:
        size = path.stat().st_size
    except OSError:
        size = meta.int("System:FileSize") or 0

    info = FileInfo(
        path=str(path),
        name=path.name,
        size_bytes=size,
        size_human=human_bytes(size),
        mime_type=meta.str("File:MIMEType"),
        file_type=meta.str("File:FileType"),
        file_type_extension=meta.str("File:FileTypeExtension"),
        extension=path.suffix.lstrip(".").lower() or None,
        modified=meta.str("System:FileModifyDate"),
    )
    if options.hash_file:
        # An unreadable file still deserves a report; the hashes are a bonus.
        with contextlib.suppress(OSError):
            info.sha256, info.md5 = hash_file(path)
    return info


def extract_image(meta: Metadata) -> ImageInfo:
    info = ImageInfo(
        width=meta.int("File:ImageWidth", "ExifIFD:ExifImageWidth", "PNG:ImageWidth"),
        height=meta.int("File:ImageHeight", "ExifIFD:ExifImageHeight", "PNG:ImageHeight"),
        megapixels=meta.float("Composite:Megapixels"),
        orientation=meta.str("IFD0:Orientation"),
        encoding_process=meta.str("File:EncodingProcess"),
        bits_per_sample=meta.int("File:BitsPerSample"),
        color_components=meta.int("File:ColorComponents"),
        subsampling=meta.str("File:YCbCrSubSampling"),
        color_space=meta.str("ExifIFD:ColorSpace", "ICC-header:ColorSpaceData"),
        icc_profile=meta.str("ICC_Profile:ProfileDescription"),
    )
    thumbnail_length = meta.int("IFD1:ThumbnailLength")
    info.has_thumbnail = bool(thumbnail_length) or meta.has("ThumbnailImage")
    info.thumbnail_size = thumbnail_length
    return info


def _makernote_vendor(meta: Metadata) -> str | None:
    """Which vendor's MakerNote block is present, if any.

    MakerNotes are the strongest single signal that a file came straight off a
    camera: almost every re-encoding pipeline discards them.
    """
    known = {
        "Apple": "Apple",
        "Canon": "Canon",
        "Nikon": "Nikon",
        "Sony": "Sony",
        "Samsung": "Samsung",
        "Panasonic": "Panasonic",
        "Olympus": "Olympus",
        "Fujifilm": "Fujifilm",
        "Pentax": "Pentax",
        "Ricoh": "Ricoh",
        "Sigma": "Sigma",
        "Leica": "Leica",
        "Casio": "Casio",
        "Kodak": "Kodak",
        "Minolta": "Minolta",
        "GoPro": "GoPro",
        "DJI": "DJI",
        "Motorola": "Motorola",
        "HP": "HP",
        "Sanyo": "Sanyo",
        "Reconyx": "Reconyx",
        "PhaseOne": "Phase One",
    }
    present = meta.group_names()
    for group, name in known.items():
        if group in present:
            return name
    if meta.has("MakerNoteVersion", "MakerNoteUnknownText", "MakerNoteUnknownBinary"):
        return "Unknown vendor"
    return None


def _capture_mode_keys(meta: Metadata) -> list[str]:
    """Catalogue keys describing how the shot was taken.

    Returned as keys rather than words: this is findpic's own phrasing, unlike
    the enum values exiftool decodes, so it has to translate.
    """
    parts: list[str] = []

    capture_type = meta.int("Apple:ImageCaptureType")
    if capture_type is not None and capture_type in APPLE_IMAGE_CAPTURE_TYPE:
        parts.append(APPLE_IMAGE_CAPTURE_TYPE[capture_type])

    camera_type = meta.int("Apple:CameraType")
    if camera_type is not None and camera_type in APPLE_CAMERA_TYPE:
        parts.append(APPLE_CAMERA_TYPE[camera_type])

    if meta.has("Apple:ContentIdentifier", "Apple:MediaGroupUUID"):
        parts.append("live_photo")
    if meta.has("XMP-GCamera:MotionPhoto", "XMP-GCamera:MicroVideo"):
        parts.append("motion_photo")
    if meta.has("XMP-GCamera:HdrPlusMakernote"):
        parts.append("hdr_plus")
    if "composite" in (meta.str("ExifIFD:CompositeImage") or "").lower():
        parts.append("composite")

    return list(dict.fromkeys(parts))


def extract_device(meta: Metadata) -> DeviceInfo:
    make = meta.str("IFD0:Make", "Make")
    model = meta.str("IFD0:Model", "Model")
    software = meta.str("IFD0:Software", "Software", "XMP-tiff:Software")

    device = DeviceInfo(
        make=make.strip() if make else None,
        model=model.strip() if model else None,
        software_raw=software,
        host_computer=meta.str("IFD0:HostComputer"),
        lens_make=meta.str("ExifIFD:LensMake"),
        lens_model=meta.str("ExifIFD:LensModel", "Composite:LensID", "XMP-aux:Lens"),
        lens_id=meta.str("Composite:LensID"),
        lens_serial=meta.str("ExifIFD:LensSerialNumber"),
        body_serial=meta.str(
            "ExifIFD:SerialNumber",
            "ExifIFD:BodySerialNumber",
            "SerialNumber",
            "InternalSerialNumber",
        ),
        owner=meta.str("ExifIFD:OwnerName", "ExifIFD:CameraOwnerName", "IFD0:Artist"),
    )

    device.makernote_vendor = _makernote_vendor(meta)
    device.has_makernotes = device.makernote_vendor is not None
    device.editor = match_editor(software) or match_editor(
        meta.str("XMP-xmp:CreatorTool", "ProcessingSoftware", "HistorySoftwareAgent")
    )
    device.capture_mode_keys = _capture_mode_keys(meta)
    device.os = _operating_system(meta, device)
    device.uptime_seconds = _uptime_seconds(meta)
    return device


def _operating_system(meta: Metadata, device: DeviceInfo) -> str | None:
    """Read the OS out of Software, which every vendor formats differently."""
    software = (device.software_raw or "").strip()
    make = (device.make or "").lower()

    if software and BARE_VERSION.match(software):
        # Apple writes only the version number. On other makes a bare version is
        # ambiguous, so we report it without naming an OS.
        if "apple" in make:
            return f"{apple_os_name(device.model)} {software}"
        return f"Firmware/OS version {software}"

    if not software:
        return None

    lowered = software.lower()
    if lowered.startswith("android"):
        return software
    if "windows" in lowered:
        return software
    if "mac os" in lowered or "macos" in lowered or "darwin" in lowered:
        return software
    if "linux" in lowered:
        return software
    # A Software value we recognise as an app is reported as an editor, not an OS.
    if match_editor(software):
        return None
    return software


def _uptime_seconds(meta: Metadata) -> float | None:
    """Apple records how long the device had been running when the shot fired.

    Returned in seconds rather than as text: exiftool's own rendering is English
    only, and this number ends up in a sentence that has to translate.
    """
    value = meta.float("Apple:RunTimeValue")
    scale = meta.float("Apple:RunTimeScale")
    if value and scale:
        return value / scale
    return None


def extract_capture(meta: Metadata) -> CaptureInfo:
    taken = parse_exif_datetime(
        meta.get("Composite:SubSecDateTimeOriginal")
    ) or parse_exif_datetime(meta.get("ExifIFD:DateTimeOriginal"))
    digitized = parse_exif_datetime(meta.get("Composite:SubSecCreateDate")) or parse_exif_datetime(
        meta.get("ExifIFD:CreateDate")
    )
    modified = parse_exif_datetime(meta.get("Composite:SubSecModifyDate")) or parse_exif_datetime(
        meta.get("IFD0:ModifyDate")
    )

    offset = meta.str("ExifIFD:OffsetTimeOriginal", "ExifIFD:OffsetTime")

    capture = CaptureInfo(
        taken=format_datetime(taken),
        taken_offset=offset,
        taken_subsec=meta.str("ExifIFD:SubSecTimeOriginal"),
        digitized=format_datetime(digitized),
        modified=format_datetime(modified),
        iso=meta.int("ExifIFD:ISO", "ISO"),
        f_number=meta.float("ExifIFD:FNumber", "Composite:Aperture"),
        exposure_time=meta.str("Composite:ShutterSpeed", "ExifIFD:ExposureTime"),
        flash=meta.str("ExifIFD:Flash"),
        exposure_program=meta.str("ExifIFD:ExposureProgram"),
        metering_mode=meta.str("ExifIFD:MeteringMode"),
        white_balance=meta.str("ExifIFD:WhiteBalance"),
        scene_capture_type=meta.str("ExifIFD:SceneCaptureType"),
        light_value=meta.float("Composite:LightValue"),
    )

    capture.modified_matches_taken = same_moment(modified, taken)
    capture.digitised_differs = same_moment(digitized, taken) is False

    # Stored as bare numbers: "mm" is English, and the CLI and the bot attach
    # their own translated unit.
    focal = meta.float("ExifIFD:FocalLength")
    if focal:
        capture.focal_mm = focal
        capture.focal_length = f"{focal:g}"
    focal35 = meta.float("ExifIFD:FocalLengthIn35mmFormat")
    if focal35:
        capture.focal_35mm = focal35
        capture.focal_length_35mm = f"{focal35:g}"

    if offset:
        capture.timezone_source = "OffsetTimeOriginal tag"
        capture.inferred_utc_offset = offset
    elif taken is not None and taken.tzinfo is not None:
        capture.timezone_source = "timestamp"
        capture.inferred_utc_offset = format_datetime(taken)[-6:] if taken else None

    gps_date = meta.str("GPS:GPSDateStamp")
    gps_time = meta.str("GPS:GPSTimeStamp")
    if gps_date and gps_time:
        capture.gps_utc = f"{gps_date.replace(':', '-')} {gps_time} UTC"
    elif gps_date:
        capture.gps_utc = f"{gps_date.replace(':', '-')} UTC"

    return capture


def extract_location(meta: Metadata) -> LocationInfo:
    latitude = meta.float("Composite:GPSLatitude", "GPS:GPSLatitude")
    longitude = meta.float("Composite:GPSLongitude", "GPS:GPSLongitude")

    location = LocationInfo(
        latitude=latitude,
        longitude=longitude,
        altitude_m=meta.float("Composite:GPSAltitude", "GPS:GPSAltitude"),
        altitude_ref=meta.str("GPS:GPSAltitudeRef"),
        accuracy_m=meta.float("GPS:GPSHPositioningError"),
        direction_deg=meta.float("GPS:GPSImgDirection"),
        direction_ref=meta.str("GPS:GPSImgDirectionRef"),
        dest_bearing_deg=meta.float("GPS:GPSDestBearing"),
        speed=meta.float("GPS:GPSSpeed"),
        speed_ref=meta.str("GPS:GPSSpeedRef"),
        processing_method=meta.str("GPS:GPSProcessingMethod"),
        satellites=meta.str("GPS:GPSSatellites"),
        dop=meta.float("GPS:GPSDOP"),
        datestamp=meta.str("GPS:GPSDateStamp"),
        timestamp=meta.str("GPS:GPSTimeStamp"),
    )
    if location.present:
        location.dms = coords_to_dms(location.latitude, location.longitude)  # type: ignore[arg-type]
    return location


def resolve_place(location: LocationInfo, geocoder: Geocoder) -> None:
    """Fill in the human place name, in place. Failures are recorded, not raised."""
    if not location.present:
        return
    place, error = geocoder.reverse(location.latitude, location.longitude)  # type: ignore[arg-type]
    if place is not None:
        location.place = place.short_name
        location.place_detail = place.to_dict()
    elif error:
        location.geocode_error = error


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _struct_get(source: Any, *names: str) -> Any:
    """Fetch from an exiftool struct, ignoring XMP namespace prefixes.

    Struct members come back namespace-qualified (``XMP-apple-fi:ConfidenceLevel``)
    and the prefix varies by vendor, so match on the bare member name.
    """
    if not isinstance(source, dict):
        return None
    wanted = {n.lower() for n in names}
    for key, value in source.items():
        if key.split(":", 1)[-1].lower() in wanted:
            return value
    return None


def extract_people(meta: Metadata) -> list[PersonRegion]:
    """Face and person regions written by the camera or a photo manager.

    Two shapes exist: the MWG region structure (Apple, Adobe, most cameras) and
    Microsoft's People tags, which can carry actual names.
    """
    people: list[PersonRegion] = []

    region_info = meta.get("XMP-mwg-rs:RegionInfo", "RegionInfo")
    if isinstance(region_info, dict):
        entries = region_info.get("RegionList") or []
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            area = entry.get("Area") if isinstance(entry.get("Area"), dict) else {}
            people.append(
                PersonRegion(
                    kind=str(_struct_get(entry, "Type") or "Face"),
                    name=_struct_get(entry, "Name") or None,
                    x=_as_float(_struct_get(area, "X")),
                    y=_as_float(_struct_get(area, "Y")),
                    w=_as_float(_struct_get(area, "W")),
                    h=_as_float(_struct_get(area, "H")),
                    confidence=_as_float(_struct_get(entry.get("Extensions"), "ConfidenceLevel")),
                )
            )

    if not people:
        # Flat fallback: the numeric pass leaves these unstructured.
        count = len(meta.keys_for("RegionType")) or len(meta.keys_for("RegionAreaX"))
        for _ in range(count):
            people.append(PersonRegion(kind=meta.str("RegionType") or "Face"))

    names = meta.get("XMP-MP:RegionPersonDisplayName", "RegionPersonDisplayName")
    if names:
        listed = names if isinstance(names, list) else [names]
        for index, name in enumerate(str(n) for n in listed if n):
            if index < len(people) and not people[index].name:
                people[index].name = name
            elif index >= len(people):
                people.append(PersonRegion(kind="Person", name=name))

    listed_people = meta.get("XMP-iptcExt:PersonInImage", "PersonInImage")
    if listed_people:
        entries = listed_people if isinstance(listed_people, list) else [listed_people]
        known = {p.name for p in people if p.name}
        for name in (str(n) for n in entries if n):
            if name not in known:
                people.append(PersonRegion(kind="Person", name=name))

    return people
