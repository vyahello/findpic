"""Data model for a findpic analysis.

Everything the analyzers produce lands in a `Report`. Renderers (terminal, JSON,
and later the Telegram bot) consume `Report` and nothing else, so adding an
output format never means touching analysis code.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .i18n import Translator


class Severity(enum.Enum):
    """How much the user should care about a finding."""

    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "notice": 1, "warning": 2, "critical": 3}[self.value]


class Confidence(enum.Enum):
    """How sure we are a finding is real. Metadata lies; we say when it might."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


class Category(enum.Enum):
    """Which of the three independent axes a finding speaks to.

    DEVICE/PLATFORM/AI findings are contextual: they inform the reader but do not
    by themselves move a verdict score.
    """

    AUTHENTICITY = "authenticity"
    DEVICE = "device"
    PLATFORM = "platform"
    AI = "ai"
    PRIVACY = "privacy"
    STRUCTURAL = "structural"


@dataclass(frozen=True)
class Finding:
    """One thing we noticed.

    A finding is language-neutral: it holds a stable ``id`` and the facts that go
    into the sentence, never the sentence itself. ``title()`` and ``detail()``
    build the text from the catalogue at render time, keyed by the id. That is
    what lets the same analysis print in English or Ukrainian without the rules
    knowing either language exists.

    ``variant`` and ``detail_variant`` select between phrasings of the same
    finding — a crop and a resize are the same rule but not the same sentence.
    """

    id: str
    category: Category
    severity: Severity
    confidence: Confidence
    params: dict[str, Any] = field(default_factory=dict)
    #: Drives plural selection, and exposed to templates as ``{count}``.
    count: int | None = None
    variant: str | None = None
    detail_variant: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    weight: float = 0.0
    #: Shell commands are never translated — they must run as written.
    remediation: str | None = None

    @property
    def title_key(self) -> str:
        base = f"finding.{self.id}.title"
        return f"{base}.{self.variant}" if self.variant else base

    @property
    def detail_key(self) -> str:
        base = f"finding.{self.id}.detail"
        return f"{base}.{self.detail_variant}" if self.detail_variant else base

    def resolve_params(self, translator: Translator) -> dict[str, Any]:
        """Translate parameters that are lists of catalogue keys.

        A rule that wants to say "missing: sub-second timestamps, lens
        description" cannot build that phrase — the words are language-specific
        and so is the separator. Two conventions handle it:

        ``{prefix}_keys``
            A list of keys. Each is translated and joined, producing ``{prefix}``.

        ``{prefix}_pairs``
            A list of ``(key, value)``. If the catalogue entry has a ``{value}``
            slot the value goes inside it ("altitude 325 m"); otherwise the pair
            is rendered as a label and a value ("Author: Jane").

        ``{prefix}_bytes``
            A byte count, formatted with translated units ("3 KB" / "3 КБ").

        ``{prefix}_seconds``
            A duration, formatted with a translated, correctly-pluralised day
            count ("2 days 13:14:39" / "2 дні 13:14:39").
        """
        resolved: dict[str, Any] = {}
        separator = translator.get("ui.list.separator")

        for name, value in self.params.items():
            if name.endswith("_keys") and isinstance(value, (list, tuple)):
                prefix = name[: -len("_keys")]
                resolved[prefix] = separator.join(
                    translator.get(f"{prefix}.{item}") for item in value
                )
            elif name.endswith("_pairs") and isinstance(value, (list, tuple)):
                prefix = name[: -len("_pairs")]
                rendered: list[str] = []
                for pair in value:
                    key, paired = pair[0], pair[1]
                    entry = f"{prefix}.{key}"
                    if translator.has_placeholder(entry, "value"):
                        rendered.append(translator.get(entry, value=paired))
                    else:
                        rendered.append(
                            translator.get(
                                "ui.list.pair",
                                label=translator.get(entry),
                                value=paired,
                            )
                        )
                resolved[prefix] = separator.join(rendered)
            elif name.endswith("_bytes") and isinstance(value, (int, float)):
                resolved[name[: -len("_bytes")]] = translator.bytes(value)
                resolved[name] = value
            elif name.endswith("_seconds") and isinstance(value, (int, float)):
                resolved[name[: -len("_seconds")]] = translator.duration(value)
                resolved[name] = value
            else:
                resolved[name] = value
        return resolved

    def title(self, translator: Translator) -> str:
        return translator.get(self.title_key, self.count, **self.resolve_params(translator))

    def detail(self, translator: Translator) -> str:
        if not translator.has(self.detail_key):
            return ""
        return translator.get(self.detail_key, self.count, **self.resolve_params(translator))

    def to_dict(self, translator: Translator) -> dict[str, Any]:
        return {
            # `id` is the stable contract for anything consuming the JSON;
            # `title` and `detail` follow the chosen language.
            "id": self.id,
            "category": self.category.value,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "title": self.title(translator),
            "detail": self.detail(translator),
            "evidence": self.evidence,
            "weight": self.weight,
            "remediation": self.remediation,
        }


@dataclass
class FileInfo:
    """The file as the filesystem sees it, plus content hashes."""

    path: str
    name: str
    size_bytes: int = 0
    size_human: str = ""
    mime_type: str | None = None
    file_type: str | None = None
    file_type_extension: str | None = None
    extension: str | None = None
    sha256: str | None = None
    md5: str | None = None
    modified: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ImageInfo:
    """Pixel-level facts, independent of who took the picture."""

    width: int | None = None
    height: int | None = None
    megapixels: float | None = None
    orientation: str | None = None
    encoding_process: str | None = None
    bits_per_sample: int | None = None
    color_components: int | None = None
    subsampling: str | None = None
    color_space: str | None = None
    icc_profile: str | None = None
    has_thumbnail: bool = False
    thumbnail_size: int | None = None

    @property
    def dimensions(self) -> str | None:
        if self.width and self.height:
            return f"{self.width} × {self.height}"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "dimensions": self.dimensions}


@dataclass
class DeviceInfo:
    """What made the picture. `os` is the best-effort human reading of Software."""

    make: str | None = None
    model: str | None = None
    os: str | None = None
    software_raw: str | None = None
    host_computer: str | None = None
    lens_make: str | None = None
    lens_model: str | None = None
    lens_id: str | None = None
    lens_serial: str | None = None
    body_serial: str | None = None
    owner: str | None = None
    capture_mode_keys: list[str] = field(default_factory=list)
    #: Seconds the device had been running when the shutter fired. Stored raw so
    #: the phrasing ("2 days 13:14:39") can be built in the reader's language.
    uptime_seconds: float | None = None
    editor: str | None = None
    has_makernotes: bool = False
    makernote_vendor: str | None = None

    @property
    def label(self) -> str:
        parts = [p for p in (self.make, self.model) if p]
        if self.make and self.model and self.model.lower().startswith(self.make.lower()):
            parts = [self.model]
        return " ".join(parts) if parts else "Unknown device"

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "label": self.label}


@dataclass
class CaptureInfo:
    """When the shutter fired, and every timestamp that disagrees with it."""

    taken: str | None = None
    taken_offset: str | None = None
    taken_subsec: str | None = None
    digitized: str | None = None
    modified: str | None = None
    #: Whether ModifyDate describes the same moment as DateTimeOriginal. None
    #: when one of them is missing. Computed once so the rule and the renderer
    #: cannot disagree, and so neither compares formatted strings.
    modified_matches_taken: bool | None = None
    gps_utc: str | None = None
    timezone_source: str | None = None
    inferred_utc_offset: str | None = None
    iso: int | None = None
    f_number: float | None = None
    exposure_time: str | None = None
    focal_length: str | None = None
    focal_length_35mm: str | None = None
    flash: str | None = None
    exposure_program: str | None = None
    metering_mode: str | None = None
    white_balance: str | None = None
    scene_capture_type: str | None = None
    light_value: float | None = None

    #: Whether the digitised time is a different moment from the capture time.
    #: Computed alongside `modified_matches_taken` so neither compares strings —
    #: one timestamp often carries a UTC offset while its sibling does not.
    digitised_differs: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LocationInfo:
    """Where the picture was taken, rendered every way a human might want it."""

    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    altitude_ref: str | None = None
    accuracy_m: float | None = None
    direction_deg: float | None = None
    direction_ref: str | None = None
    dest_bearing_deg: float | None = None
    speed: float | None = None
    speed_ref: str | None = None
    processing_method: str | None = None
    satellites: str | None = None
    dop: float | None = None
    datestamp: str | None = None
    timestamp: str | None = None
    dms: str | None = None
    place: str | None = None
    place_detail: dict[str, Any] = field(default_factory=dict)
    geocode_error: str | None = None

    @property
    def present(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def decimal(self) -> str | None:
        if not self.present:
            return None
        return f"{self.latitude:.6f}, {self.longitude:.6f}"

    @property
    def geo_uri(self) -> str | None:
        if not self.present:
            return None
        return f"geo:{self.latitude:.6f},{self.longitude:.6f}"

    @property
    def osm_url(self) -> str | None:
        if not self.present:
            return None
        return (
            f"https://www.openstreetmap.org/?mlat={self.latitude:.6f}"
            f"&mlon={self.longitude:.6f}#map=17/{self.latitude:.6f}/{self.longitude:.6f}"
        )

    @property
    def google_url(self) -> str | None:
        if not self.present:
            return None
        return f"https://www.google.com/maps?q={self.latitude:.6f},{self.longitude:.6f}"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "decimal": self.decimal,
            "geo_uri": self.geo_uri,
            "osm_url": self.osm_url,
            "google_url": self.google_url,
        }


@dataclass
class PersonRegion:
    """A face (or named person) the camera or an app tagged in the frame."""

    kind: str = "Face"
    name: str | None = None
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class VerdictLevel(enum.Enum):
    """Verdict bands. UNKNOWN is a first-class answer, not a failure."""

    UNKNOWN = "unknown"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    BAD = "bad"

    @property
    def rank(self) -> int:
        return {"unknown": -1, "good": 0, "fair": 1, "poor": 2, "bad": 3}[self.value]


@dataclass
class Verdict:
    """One of the three independent axes, with the findings that produced it.

    Like :class:`Finding`, this stays language-neutral: the axis and level pick
    the catalogue keys, and ``reasons`` holds the findings themselves so their
    sentences render in whatever language the report is asked for.
    """

    axis: str
    level: VerdictLevel
    score: float = 0.0
    reasons: list[Finding] = field(default_factory=list)
    #: Picks between phrasings of the same level (an empty file versus a
    #: stripped one both read CLEAN, for different reasons).
    summary_variant: str | None = None

    @property
    def label_key(self) -> str:
        return f"verdict.{self.axis}.{self.level.value}.label"

    @property
    def summary_key(self) -> str:
        base = f"verdict.{self.axis}.{self.level.value}.summary"
        return f"{base}_{self.summary_variant}" if self.summary_variant else base

    def label(self, translator: Translator) -> str:
        return translator.get(self.label_key)

    def summary(self, translator: Translator) -> str:
        key = self.summary_key
        if not translator.has(key):
            key = f"verdict.{self.axis}.{self.level.value}.summary"
        return translator.get(key)

    def reason_lines(self, translator: Translator) -> list[str]:
        return [finding.title(translator) for finding in self.reasons]

    def to_dict(self, translator: Translator) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "level": self.level.value,
            "label": self.label(translator),
            "summary": self.summary(translator),
            "score": round(self.score, 1),
            "reasons": self.reason_lines(translator),
        }


@dataclass
class Report:
    """The complete analysis of one file."""

    file: FileInfo
    image: ImageInfo = field(default_factory=ImageInfo)
    device: DeviceInfo = field(default_factory=DeviceInfo)
    capture: CaptureInfo = field(default_factory=CaptureInfo)
    location: LocationInfo = field(default_factory=LocationInfo)
    people: list[PersonRegion] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    tag_count: int = 0
    groups: dict[str, int] = field(default_factory=dict)
    exiftool_warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    #: The language this report renders in. Analysis is language-neutral; this
    #: is only consulted when text is produced.
    translator: Translator = field(default_factory=Translator)

    def by_category(self, category: Category) -> list[Finding]:
        return [f for f in self.findings if f.category is category]

    @property
    def sorted_findings(self) -> list[Finding]:
        return sorted(
            self.findings,
            key=lambda f: (-f.severity.rank, -f.confidence.rank, f.id),
        )

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        translator = self.translator
        out: dict[str, Any] = {
            "language": translator.language,
            "file": self.file.to_dict(),
            "image": self.image.to_dict(),
            "device": self.device.to_dict(),
            "capture": self.capture.to_dict(),
            "location": self.location.to_dict(),
            "people": [p.to_dict() for p in self.people],
            "verdicts": {k: v.to_dict(translator) for k, v in self.verdicts.items()},
            "findings": [f.to_dict(translator) for f in self.sorted_findings],
            "tag_count": self.tag_count,
            "groups": self.groups,
            "exiftool_warnings": self.exiftool_warnings,
            "errors": self.errors,
        }
        if include_raw:
            out["raw"] = self.raw
        return out
