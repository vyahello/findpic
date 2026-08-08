"""Shared state handed to every analysis rule."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..container import ContainerScan
from ..exif import Metadata
from ..models import (
    CaptureInfo,
    DeviceInfo,
    FileInfo,
    ImageInfo,
    LocationInfo,
    PersonRegion,
)


@dataclass
class AnalysisOptions:
    """Knobs the CLI and the bot both need to set."""

    geocode: bool = True
    #: Drives both the report language and the language place names come back in.
    language: str = "en"
    hash_file: bool = True
    #: Walk the container structure to find data appended past the image's end.
    scan_container: bool = True


@dataclass
class Context:
    """Everything a rule may look at.

    Extractors populate the structured fields first; rules then run against both
    the raw :class:`Metadata` and those already-normalised fields, so a rule can
    say "the capture has no timezone" without re-parsing tags.
    """

    meta: Metadata
    path: Path
    options: AnalysisOptions = field(default_factory=AnalysisOptions)
    file: FileInfo = field(default_factory=lambda: FileInfo(path="", name=""))
    image: ImageInfo = field(default_factory=ImageInfo)
    device: DeviceInfo = field(default_factory=DeviceInfo)
    capture: CaptureInfo = field(default_factory=CaptureInfo)
    location: LocationInfo = field(default_factory=LocationInfo)
    people: list[PersonRegion] = field(default_factory=list)
    #: Result of walking the file's container structure, when enabled.
    container: ContainerScan | None = None

    @property
    def has_exif(self) -> bool:
        return self.meta.has_group("IFD0", "ExifIFD", "IFD1", "XMP-exif")

    @property
    def has_camera_identity(self) -> bool:
        return bool(self.device.make or self.device.model)
