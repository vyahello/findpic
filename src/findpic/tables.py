"""Lookup tables: raw tag values in, human meaning out.

Kept in one module so the knowledge is auditable and easy to extend without
touching analysis logic. Where a value is a community reverse-engineering result
rather than a documented one, it is marked — findpic would rather say "unknown"
than assert something it cannot stand behind.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- editing apps

#: Substring (lowercased) -> friendly editor name. Matched against Software,
#: CreatorTool, ProcessingSoftware and HistorySoftwareAgent.
EDITOR_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("adobe photoshop lightroom", "Adobe Lightroom"),
    ("photoshop lightroom", "Adobe Lightroom"),
    ("adobe photoshop", "Adobe Photoshop"),
    ("photoshop express", "Photoshop Express"),
    ("photoshop", "Adobe Photoshop"),
    ("lightroom", "Adobe Lightroom"),
    ("adobe illustrator", "Adobe Illustrator"),
    ("adobe indesign", "Adobe InDesign"),
    ("adobe firefly", "Adobe Firefly"),
    ("camera raw", "Adobe Camera Raw"),
    ("gimp", "GIMP"),
    ("krita", "Krita"),
    ("inkscape", "Inkscape"),
    ("paint.net", "Paint.NET"),
    ("paintshop", "PaintShop Pro"),
    ("pixelmator", "Pixelmator"),
    ("affinity photo", "Affinity Photo"),
    ("affinity designer", "Affinity Designer"),
    ("capture one", "Capture One"),
    ("darktable", "darktable"),
    ("rawtherapee", "RawTherapee"),
    ("dxo", "DxO PhotoLab"),
    ("luminar", "Luminar"),
    ("acdsee", "ACDSee"),
    ("corel", "Corel"),
    ("snapseed", "Snapseed"),
    ("picsart", "PicsArt"),
    ("facetune", "Facetune"),
    ("vsco", "VSCO"),
    ("lensa", "Lensa"),
    ("remini", "Remini"),
    ("topaz", "Topaz Labs"),
    ("gigapixel", "Topaz Gigapixel"),
    ("canva", "Canva"),
    ("figma", "Figma"),
    ("sketch", "Sketch"),
    ("instagram", "Instagram"),
    ("prisma", "Prisma"),
    ("aviary", "Aviary"),
    ("befunky", "BeFunky"),
    ("fotor", "Fotor"),
    ("polarr", "Polarr"),
    ("imagemagick", "ImageMagick"),
    ("graphicsmagick", "GraphicsMagick"),
    ("irfanview", "IrfanView"),
    ("xnview", "XnView"),
    ("faststone", "FastStone"),
    ("windows photo editor", "Windows Photo Editor"),
    ("microsoft windows photo viewer", "Windows Photo Viewer"),
    ("paint 3d", "Paint 3D"),
    ("google photos", "Google Photos"),
    ("picasa", "Picasa"),
    ("photos 1.", "Apple Photos"),
    ("photos 2.", "Apple Photos"),
    ("photos 3.", "Apple Photos"),
    ("photos 4.", "Apple Photos"),
    ("photos 5.", "Apple Photos"),
    ("photos 6.", "Apple Photos"),
    ("photos 7.", "Apple Photos"),
    ("photos 8.", "Apple Photos"),
    ("preview.app", "Apple Preview"),
    ("quicktime", "QuickTime"),
    ("pillow", "Python Pillow"),
    ("python-imaging", "Python Pillow"),
    ("libwebp", "libwebp"),
    ("ffmpeg", "FFmpeg"),
    ("lavc", "FFmpeg"),
    ("gd-jpeg", "PHP GD"),
    ("php", "PHP image library"),
    ("skia", "Skia (browser/Android canvas)"),
    ("chrome", "Chrome"),
    ("html2canvas", "html2canvas"),
    ("photoscape", "PhotoScape"),
    ("photopea", "Photopea"),
    ("pixlr", "Pixlr"),
    ("meitu", "Meitu"),
    ("beautyplus", "BeautyPlus"),
    ("airbrush", "AirBrush"),
    ("youcam", "YouCam Perfect"),
)

#: Apps whose presence means the pixels were retouched by a person, as opposed
#: to a pipeline that merely re-encoded the file.
RETOUCH_EDITORS = frozenset(
    {
        "Adobe Photoshop",
        "Photoshop Express",
        "Adobe Lightroom",
        "GIMP",
        "Krita",
        "Pixelmator",
        "Affinity Photo",
        "Snapseed",
        "PicsArt",
        "Facetune",
        "VSCO",
        "Lensa",
        "Remini",
        "Topaz Labs",
        "Topaz Gigapixel",
        "Photopea",
        "Pixlr",
        "Meitu",
        "BeautyPlus",
        "AirBrush",
        "YouCam Perfect",
        "Paint.NET",
        "PaintShop Pro",
        "Luminar",
        "Capture One",
        "darktable",
        "RawTherapee",
        "DxO PhotoLab",
    }
)


def match_editor(value: str | None) -> str | None:
    """Return the friendly editor name for a Software-style string."""
    if not value:
        return None
    lowered = value.lower()
    for needle, name in EDITOR_SIGNATURES:
        if needle in lowered:
            return name
    return None


#: Desktop software whose JPEG quantization tables exiftool can fingerprint.
#: Only these count as evidence: a camera's own tables are unremarkable, and
#: `Independent JPEG Group` covers libjpeg, which is used by so much server-side
#: tooling that naming it would be noise rather than a finding.
DIGEST_EDITORS: tuple[str, ...] = (
    "Adobe Photoshop",
    "Adobe Lightroom",
    "Adobe Photoshop Express",
    "Corel Paint Shop Pro",
    "ACD Systems",
    "Nikon Capture NX",
    "Canon Digital Photo Professional",
    "Sony Image Data Suite",
    "Apple Aperture",
    "GIMP",
    "Picasa",
    "FixFoto",
    "StereoPhoto Maker",
)


#: libjpeg is used by ImageMagick, Pillow, GIMP and most server-side pipelines.
#: It proves re-encoding but names no particular application, so it is reported
#: as its own, weaker finding rather than being passed off as an editor.
GENERIC_DIGEST = "Independent JPEG Group"


def match_jpeg_digest(value: str | None) -> tuple[str, str] | None:
    """Identify the encoder behind a ``File:JPEGDigest`` label.

    Returns ``(name, variant)`` where variant is ``editor`` for a named
    application or ``library`` for a generic encoder.

    exiftool returns ``Unknown (md5:…)`` for anything not in its database, which
    is the *expected* result for modern phones — their tables were never
    catalogued. That is not suspicious and must never be reported as such.
    """
    if not value or value.startswith("Unknown"):
        return None
    for name in DIGEST_EDITORS:
        if value.startswith(name):
            return name, "editor"
    if value.startswith(GENERIC_DIGEST):
        return GENERIC_DIGEST, "library"
    return None


# ------------------------------------------------------------ operating system

#: A bare dotted version with no vendor words — how Apple writes the OS version.
BARE_VERSION = re.compile(r"^\d+(\.\d+){0,3}$")

#: Android build fingerprints look like G991BXXU3AUKA / RP1A.200720.011.
ANDROID_BUILD = re.compile(r"^[A-Z]{2,3}\d[A-Z]\.\d{6}\.\d{2,3}")
SAMSUNG_FIRMWARE = re.compile(r"^[A-Z]\d{3}[A-Z]{0,2}[A-Z0-9]{6,}$")


def apple_os_name(model: str | None) -> str:
    """Apple stores only the version number, so the product line names the OS."""
    text = (model or "").lower()
    if "ipad" in text:
        return "iPadOS"
    if "watch" in text:
        return "watchOS"
    if "ipod" in text:
        return "iOS"
    if "iphone" in text:
        return "iOS"
    if "mac" in text or "imac" in text:
        return "macOS"
    return "iOS/iPadOS"


# ------------------------------------------------------- Apple MakerNote codes

#: Apple's MakerNote is undocumented. exiftool decodes what the community has
#: pinned down; everything here is community-derived, so callers should present
#: it as an interpretation and fall back to the raw number when absent.
#: Apple's ``ImageCaptureType``, again exactly as Apple.pm decodes it.
#:
#: exiftool's own comment reads "seen: 1,2,3,4,5,10,12" and its table decodes
#: only 1, 2, 10, 11 and 12 — 3, 4 and 5 are values that turn up in real files
#: and whose meaning nobody has pinned down. findpic assigned them meanings
#: anyway, so samples/IMG_1312.JPG (which carries 5) was reported as a plain
#: "Photo" on the strength of a guess.
#: What exiftool can put in ``GPSSpeedRef``, mapped to a unit. Both the raw
#: single letters of the numeric pass and the words its PrintConv produces.
#:
#: A table because the previous prefix tests read "knots" as km/h — it starts
#: with a k, and that branch was tested before the n one — so a boat at 45
#: knots was reported as 46 km/h instead of 85. "M" for miles per hour fell
#: through to km/h for the same reason.
GPS_SPEED_REF: dict[str, str] = {
    "km/h": "kmh",
    "kmh": "kmh",
    "k": "kmh",
    "mph": "mph",
    "m": "mph",
    "knots": "knots",
    "kn": "knots",
    "n": "knots",
}

#: Into kilometres per hour, which is what the bands below are expressed in.
SPEED_TO_KMH: dict[str, float] = {"kmh": 1.0, "mph": 1.609344, "knots": 1.852}

APPLE_IMAGE_CAPTURE_TYPE: dict[int, str] = {
    1: "proraw",
    2: "portrait",
    10: "photo",
    11: "manual_focus",
    12: "scene",
}

#: Apple's ``CameraType``, exactly as exiftool's own Apple.pm decodes it:
#: ``0 => Back Wide Angle, 1 => Back Normal, 6 => Front``.
#:
#: findpic had 1 as the front camera and 6 as the ultra-wide — the two that
#: matter, both inverted — plus 2 and 3 invented outright. It printed
#: "front camera" on rear-camera photographs and called selfies rear shots,
#: while the lens string in the same report said the opposite. Nothing else
#: is added here: a code exiftool declines to decode is a code nobody has
#: established the meaning of, and guessing in a forensics tool is worse than
#: staying quiet.
APPLE_CAMERA_TYPE: dict[int, str] = {
    0: "back_wide",
    1: "back",
    6: "front",
}

#: Apple's ``OISMode`` is *not* decoded by exiftool at all — its note says only
#: "seen: 2,3,5". findpic had a table claiming 0 meant off and everything else
#: meant on, which is an invention, and nothing read it: the one consumer tested
#: the tag's mere presence. Kept as a comment rather than a table because the
#: honest answer is that the tag says the device recorded a stabilisation mode,
#: not that stabilisation was active.


# ---------------------------------------------------- filename provenance hints

#: Filename shape -> where the file most likely came from. These are hints only;
#: a filename is trivially changed, so findings built on them stay low-confidence.
FILENAME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^IMG_\d{4}\.(jpe?g|heic|png)$", re.I), "apple"),
    (re.compile(r"^IMG_\d{8}_\d{6}", re.I), "android"),
    (re.compile(r"^PXL_\d{8}_\d{6,}", re.I), "pixel"),
    (re.compile(r"^DSC[_N]?\d{4,}", re.I), "camera"),
    (re.compile(r"^(DSCF|IMGP|P\d{7}|_MG_|_DSC)", re.I), "camera"),
    (re.compile(r"^GOPR\d+|^GX\d{6}", re.I), "gopro"),
    (re.compile(r"^DJI_\d+", re.I), "dji"),
    (re.compile(r"^Screenshot[_ -]", re.I), "screenshot"),
    (re.compile(r"^Screen Shot \d{4}-\d{2}-\d{2}", re.I), "macos_screenshot"),
    (re.compile(r"^photo_\d+@\d{2}-\d{2}-\d{4}", re.I), "telegram"),
    (re.compile(r"^photo_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", re.I), "telegram"),
    (re.compile(r"^IMG-\d{8}-WA\d+", re.I), "whatsapp"),
    (re.compile(r"^WhatsApp Image \d{4}-\d{2}-\d{2}", re.I), "whatsapp"),
    (re.compile(r"^FB_IMG_\d+", re.I), "facebook"),
    (re.compile(r"^received_\d+", re.I), "messenger"),
    (re.compile(r"^Snapchat-\d+", re.I), "snapchat"),
    (re.compile(r"^signal-\d{4}-\d{2}-\d{2}", re.I), "signal"),
    (re.compile(r"^viber_image_", re.I), "viber"),
    (re.compile(r"^image0*\d?\.(jpe?g|png)$", re.I), "messaging"),
    (re.compile(r"^unnamed\.(jpe?g|png)$", re.I), "email"),
    (re.compile(r"^download(\s*\(\d+\))?\.(jpe?g|png)$", re.I), "download"),
    (re.compile(r"^(images?|unknown|untitled)(\s*\(\d+\))?\.(jpe?g|png)$", re.I), "download"),
)


def match_filename(name: str) -> str | None:
    """Catalogue key for where a filename shape suggests the file came from."""
    for pattern, key in FILENAME_PATTERNS:
        if pattern.match(name):
            return key
    return None


# ------------------------------------------------------- synthetic media markers

#: IPTC's controlled vocabulary for how media was produced. A photo that declares
#: one of these is telling you outright that a model was involved.
DIGITAL_SOURCE_TYPES: dict[str, bool] = {
    "trainedalgorithmicmedia": True,
    "compositewithtrainedalgorithmicmedia": True,
    "algorithmicmedia": True,
    "algorithmicallyenhanced": True,
    "digitalcapture": False,
    "negativefilm": False,
    "positivefilm": False,
    "print": False,
    "minorhumanedits": False,
    "compositecapture": False,
    "composite": False,
    "softwareimage": False,
    "virtualrecording": False,
    "datadrivenmedia": False,
}

#: ``(needle, generator, signing_only)``, matched on word boundaries.
#:
#: ``signing_only`` marks a needle that is also an ordinary English or Spanish
#: word. "Flux", "Imagen", "Gemini", "Grok" and the rest appear in captions
#: written by people about photographs they took themselves — a Canon original
#: captioned "Sunrise over the Gemini Observatory" was being told, at WARNING,
#: that its metadata named an AI tool. Those needles are believed only where a
#: generator actually signs its work; the unambiguous ones are believed
#: anywhere.
AI_GENERATOR_SIGNATURES: tuple[tuple[str, str, bool], ...] = (
    ("midjourney", "Midjourney", False),
    ("dall-e", "OpenAI DALL·E", False),
    ("dall·e", "OpenAI DALL·E", False),
    ("openai", "OpenAI", True),
    ("chatgpt", "ChatGPT", True),
    ("stable diffusion", "Stable Diffusion", False),
    ("stablediffusion", "Stable Diffusion", False),
    ("automatic1111", "Stable Diffusion (AUTOMATIC1111)", False),
    ("comfyui", "ComfyUI", False),
    ("invokeai", "InvokeAI", False),
    ("adobe firefly", "Adobe Firefly", False),
    ("firefly", "Adobe Firefly", True),
    ("leonardo.ai", "Leonardo.Ai", False),
    ("ideogram", "Ideogram", True),
    ("flux", "Flux", True),
    ("imagen", "Google Imagen", True),
    ("gemini", "Google Gemini", True),
    ("grok", "xAI Grok", True),
    ("nightcafe", "NightCafe", False),
    ("dreamstudio", "DreamStudio", False),
    ("novelai", "NovelAI", False),
    ("craiyon", "Craiyon", True),
    ("bing image creator", "Bing Image Creator", False),
    ("designer.microsoft", "Microsoft Designer", False),
    ("recraft", "Recraft", True),
    ("playground ai", "Playground AI", False),
)


# ---------------------------------------------- exiftool's own English prose
#
# exiftool decodes these numeric tags into English sentences, and findpic
# printed them verbatim — so a Ukrainian report said "Орієнтація Rotate 90 CW"
# and "Спалах Auto, Did not fire". There is no Ukrainian language pack for
# exiftool (Lang/ ships cs de en es fi fr it ja ko nl pl ru sk sv tr zh), so
# every one of these has to live in findpic's own catalogue.
#
# Mapped by exact string rather than by slugging the value: an unmapped value
# would otherwise ask the catalogue for a key that does not exist, and a missing
# key falls through to the English catalogue while failing the "no missing
# translations" test. Out-of-range tags render as "Unknown (5)", which is
# deliberately not in any of these tables — the raw string is the honest answer.
#
# The English side is the exiftool string verbatim, so English output does not
# change and --json is untouched: extract.py keeps storing the raw value, and
# `authenticity.jpeg_progressive` still substring-matches "progressive" on it.

#: EXIF Orientation, all eight values.
ORIENTATION_KEYS: dict[str, str] = {
    "Horizontal (normal)": "exif.orientation.horizontal",
    "Mirror horizontal": "exif.orientation.mirror_horizontal",
    "Rotate 180": "exif.orientation.rotate_180",
    "Mirror vertical": "exif.orientation.mirror_vertical",
    "Mirror horizontal and rotate 270 CW": "exif.orientation.mirror_h_rotate_270",
    "Rotate 90 CW": "exif.orientation.rotate_90_cw",
    "Mirror horizontal and rotate 90 CW": "exif.orientation.mirror_h_rotate_90",
    "Rotate 270 CW": "exif.orientation.rotate_270_cw",
}

#: EXIF Flash. The bit field decodes to twenty-seven strings, and the bot
#: collapsed all of them into two sentences — losing the difference between a
#: flash the photographer switched off and one the camera decided not to use,
#: which is exactly what this row is read for — while printing nothing at all
#: for the two "no flash function" values.
FLASH_KEYS: dict[str, str] = {
    "No Flash": "exif.flash.none",
    "Fired": "exif.flash.fired",
    "Fired, Return not detected": "exif.flash.fired_no_return",
    "Fired, Return detected": "exif.flash.fired_return",
    "On, Did not fire": "exif.flash.on_no_fire",
    "On, Fired": "exif.flash.on_fired",
    "On, Return not detected": "exif.flash.on_no_return",
    "On, Return detected": "exif.flash.on_return",
    "Off, Did not fire": "exif.flash.off",
    "Off, Did not fire, Return not detected": "exif.flash.off_no_return",
    "Auto, Did not fire": "exif.flash.auto_no_fire",
    "Auto, Fired": "exif.flash.auto_fired",
    "Auto, Fired, Return not detected": "exif.flash.auto_fired_no_return",
    "Auto, Fired, Return detected": "exif.flash.auto_fired_return",
    "No flash function": "exif.flash.absent",
    "Off, No flash function": "exif.flash.absent",
    "Fired, Red-eye reduction": "exif.flash.fired_redeye",
    "Fired, Red-eye reduction, Return not detected": "exif.flash.fired_redeye_no_return",
    "Fired, Red-eye reduction, Return detected": "exif.flash.fired_redeye_return",
    "On, Red-eye reduction": "exif.flash.on_redeye",
    "On, Red-eye reduction, Return not detected": "exif.flash.on_redeye_no_return",
    "On, Red-eye reduction, Return detected": "exif.flash.on_redeye_return",
    "Off, Red-eye reduction": "exif.flash.off_redeye",
    "Auto, Did not fire, Red-eye reduction": "exif.flash.auto_no_fire_redeye",
    "Auto, Fired, Red-eye reduction": "exif.flash.auto_fired_redeye",
    "Auto, Fired, Red-eye reduction, Return not detected": "exif.flash.auto_fired_redeye_no_return",
    "Auto, Fired, Red-eye reduction, Return detected": "exif.flash.auto_fired_redeye_return",
}

#: EXIF ExposureProgram.
EXPOSURE_PROGRAM_KEYS: dict[str, str] = {
    "Not Defined": "exif.program.undefined",
    "Manual": "exif.program.manual",
    "Program AE": "exif.program.program_ae",
    "Aperture-priority AE": "exif.program.aperture_priority",
    "Shutter speed priority AE": "exif.program.shutter_priority",
    "Creative (Slow speed)": "exif.program.creative",
    "Action (High speed)": "exif.program.action",
    "Portrait": "exif.program.portrait",
    "Landscape": "exif.program.landscape",
    "Bulb": "exif.program.bulb",
}

#: JPEG SOF marker. Note exiftool's own inconsistent capitalisation of
#: "Differential" in the lossless row — these are matched byte for byte.
ENCODING_PROCESS_KEYS: dict[str, str] = {
    "Baseline DCT, Huffman coding": "exif.encoding.baseline",
    "Extended sequential DCT, Huffman coding": "exif.encoding.extended",
    "Progressive DCT, Huffman coding": "exif.encoding.progressive",
    "Lossless, Huffman coding": "exif.encoding.lossless",
    "Sequential DCT, differential Huffman coding": "exif.encoding.sequential_diff",
    "Progressive DCT, differential Huffman coding": "exif.encoding.progressive_diff",
    "Lossless, Differential Huffman coding": "exif.encoding.lossless_diff",
    "Extended sequential DCT, arithmetic coding": "exif.encoding.extended_arith",
    "Progressive DCT, arithmetic coding": "exif.encoding.progressive_arith",
    "Lossless, arithmetic coding": "exif.encoding.lossless_arith",
    "Sequential DCT, differential arithmetic coding": "exif.encoding.sequential_diff_arith",
    "Progressive DCT, differential arithmetic coding": "exif.encoding.progressive_diff_arith",
    "Lossless, differential arithmetic coding": "exif.encoding.lossless_diff_arith",
}

#: EXIF ColorSpace, only where the value is prose. "sRGB", "Adobe RGB" and
#: "Wide Gamut RGB" are the names of standards and stay as they are, as do the
#: raw four-character ICC signatures (RGB, GRAY, CMYK, Lab) that reach the same
#: field from ICC-header:ColorSpaceData.
COLOR_SPACE_KEYS: dict[str, str] = {
    "Uncalibrated": "exif.colorspace.uncalibrated",
    "ICC Profile": "exif.colorspace.icc",
}

#: exiftool's GPSSpeedRef PrintConv. The interpretation beside it already says
#: "км/год", so the raw echo put both spellings of one unit on a single line.
SPEED_REF_KEYS: dict[str, str] = {
    "km/h": "exif.speed_ref.kmh",
    "mph": "exif.speed_ref.mph",
    "knots": "exif.speed_ref.knots",
}

#: EXIF MeteringMode, WhiteBalance and SceneCaptureType — the three values of
#: the Metering row, and exiftool's English prose like every table above it.
METERING_KEYS: dict[str, str] = {
    "Unknown": "exif.metering.unknown",
    "Average": "exif.metering.average",
    "Center-weighted average": "exif.metering.center_weighted",
    "Spot": "exif.metering.spot",
    "Multi-spot": "exif.metering.multi_spot",
    "Multi-segment": "exif.metering.multi_segment",
    "Partial": "exif.metering.partial",
    "Other": "exif.metering.other",
}

WHITE_BALANCE_KEYS: dict[str, str] = {
    "Auto": "exif.white_balance.auto",
    "Manual": "exif.white_balance.manual",
}

SCENE_TYPE_KEYS: dict[str, str] = {
    "Standard": "exif.scene.standard",
    "Landscape": "exif.scene.landscape",
    "Portrait": "exif.scene.portrait",
    "Night": "exif.scene.night",
}
