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
APPLE_IMAGE_CAPTURE_TYPE: dict[int, str] = {
    1: "proraw",
    2: "portrait",
    3: "photo",
    4: "manual_focus",
    5: "photo",
    10: "photo",
    11: "manual_focus",
    12: "scene",
}

APPLE_CAMERA_TYPE: dict[int, str] = {
    0: "back_wide",
    1: "front",
    2: "back",
    3: "back_tele",
    6: "back_ultrawide",
}

APPLE_OIS_MODE: dict[int, str] = {
    0: "Off",
    1: "On",
    2: "On",
    3: "On",
}


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

#: Substring -> generator name, matched across Software/CreatorTool/XMP/comments.
AI_GENERATOR_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("midjourney", "Midjourney"),
    ("dall-e", "OpenAI DALL·E"),
    ("dall·e", "OpenAI DALL·E"),
    ("openai", "OpenAI"),
    ("chatgpt", "ChatGPT"),
    ("stable diffusion", "Stable Diffusion"),
    ("stablediffusion", "Stable Diffusion"),
    ("automatic1111", "Stable Diffusion (AUTOMATIC1111)"),
    ("comfyui", "ComfyUI"),
    ("invokeai", "InvokeAI"),
    ("adobe firefly", "Adobe Firefly"),
    ("firefly", "Adobe Firefly"),
    ("leonardo.ai", "Leonardo.Ai"),
    ("ideogram", "Ideogram"),
    ("flux", "Flux"),
    ("imagen", "Google Imagen"),
    ("gemini", "Google Gemini"),
    ("grok", "xAI Grok"),
    ("nightcafe", "NightCafe"),
    ("dreamstudio", "DreamStudio"),
    ("novelai", "NovelAI"),
    ("craiyon", "Craiyon"),
    ("bing image creator", "Bing Image Creator"),
    ("designer.microsoft", "Microsoft Designer"),
    ("recraft", "Recraft"),
    ("playground ai", "Playground AI"),
)
