"""Shared state handed to every analysis rule."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..container import ContainerScan
from ..exif import Metadata
from ..jpegprint import JpegPrint
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
    #: Read the JPEG's compression structure. Cheap, and the only thing left to
    #: read when a file's metadata has been stripped.
    fingerprint_encoder: bool = True


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
    #: How the JPEG itself was compressed. This is the only evidence that
    #: survives a metadata strip, so it is what the rules fall back on when
    #: every tag they normally read has been deleted.
    jpeg: JpegPrint | None = None
    #: The embedded preview, fingerprinted on its own. It can disagree with the
    #: image around it, and the disagreement is the finding.
    thumbnail: JpegPrint | None = None

    @property
    def has_exif(self) -> bool:
        return self.meta.has_group("IFD0", "ExifIFD", "IFD1", "XMP-exif")

    @property
    def has_camera_identity(self) -> bool:
        return bool(self.device.make or self.device.model)

    @property
    def stripped(self) -> bool:
        """Whether the identifying metadata is gone.

        Not the same as "has no Exif": a messenger leaves an Exif block behind,
        rebuilt down to a skeleton of ExifVersion and the pixel dimensions. From
        the reader's side those are the same file — nothing says who took it or
        when — so both must reach the rules that explain that.
        """
        return not self.has_camera_identity and not self.capture.taken
