"""Turn findings into three independent verdicts.

The axes are kept separate on purpose. A holiday snap straight off your phone is
a perfect original *and* a serious privacy leak; a scrubbed meme is a privacy
non-event *and* completely unverifiable. Collapsing those into one "score" would
destroy the only information the user actually wants.

Every axis can return UNKNOWN, and does so whenever the file simply does not
carry enough metadata to support a claim. Absence of evidence is reported as
absence of evidence, never as evidence of wrongdoing.

Nothing here produces text: a verdict is an axis plus a level, and the sentences
come from the message catalogue at render time.
"""

from __future__ import annotations

from ..models import Category, Finding, Severity, Verdict, VerdictLevel
from .context import Context

#: Minimum tags before we will assert anything about originality. Below this the
#: file has been stripped (or never had metadata) and the honest answer is
#: "unknown".
MIN_TAGS_FOR_JUDGEMENT = 12

#: Findings that mean the metadata contradicts *itself*, as opposed to merely
#: showing a lot of editing. Only these can reach the worst originality band —
#: "heavily edited" and "internally inconsistent" are different claims, and
#: letting ordinary edit evidence accumulate into the latter would be dishonest.
CONTRADICTION_IDS = frozenset({"authenticity.gps_time_disagrees"})

#: A score this high means edit evidence from many independent directions.
OVERWHELMING_SCORE = 90


def _band(score: float, thresholds: tuple[tuple[float, VerdictLevel], ...]) -> VerdictLevel:
    for limit, level in thresholds:
        if score < limit:
            return level
    return thresholds[-1][1]


def _reasons(findings: list[Finding], limit: int = 4) -> list[Finding]:
    ranked = sorted(
        (f for f in findings if f.weight or f.severity.rank >= Severity.NOTICE.rank),
        key=lambda f: (-f.weight, -f.severity.rank),
    )
    return ranked[:limit]


def originality_verdict(context: Context, findings: list[Finding]) -> Verdict:
    """How much the file looks like an untouched camera original."""
    relevant = [f for f in findings if f.category is Category.AUTHENTICITY]
    score = sum(f.weight for f in relevant)

    # "Is this an untouched camera original?" is unanswerable unless something
    # claims a camera or a capture time in the first place. A container that
    # merely has an Exif block — a converted TIFF, a re-wrapped PNG — has nothing
    # to be original *of*, and calling it ORIGINAL would be a false reassurance.
    judgeable = (
        context.has_exif
        and context.meta.tag_count >= MIN_TAGS_FOR_JUDGEMENT
        and (context.has_camera_identity or bool(context.capture.taken))
    )
    if not judgeable:
        return Verdict(
            axis="originality",
            level=VerdictLevel.UNKNOWN,
            score=score,
            reasons=_reasons(relevant),
        )

    contradicted = any(f.id in CONTRADICTION_IDS for f in relevant)
    if contradicted or score >= OVERWHELMING_SCORE:
        level = VerdictLevel.BAD
    else:
        level = _band(
            score,
            (
                (1, VerdictLevel.GOOD),
                (25, VerdictLevel.FAIR),
                (float("inf"), VerdictLevel.POOR),
            ),
        )
    return Verdict(axis="originality", level=level, score=score, reasons=_reasons(relevant))


def privacy_verdict(context: Context, findings: list[Finding]) -> Verdict:
    """How much the file gives away about the person who took it."""
    relevant = [f for f in findings if f.category is Category.PRIVACY]
    score = sum(f.weight for f in relevant)

    if not relevant:
        stripped = context.meta.tag_count < MIN_TAGS_FOR_JUDGEMENT
        return Verdict(
            axis="privacy",
            level=VerdictLevel.GOOD,
            score=0.0,
            summary_variant="stripped" if stripped else "empty",
        )

    level = _band(
        score,
        (
            (1, VerdictLevel.GOOD),
            (20, VerdictLevel.FAIR),
            (50, VerdictLevel.POOR),
            (float("inf"), VerdictLevel.BAD),
        ),
    )
    return Verdict(axis="privacy", level=level, score=score, reasons=_reasons(relevant))


def structure_verdict(context: Context, findings: list[Finding]) -> Verdict:
    """Whether the file itself looks like it is hiding something."""
    relevant = [f for f in findings if f.category is Category.STRUCTURAL]
    score = sum(f.weight for f in relevant)

    if not relevant:
        return Verdict(axis="structure", level=VerdictLevel.GOOD, score=0.0)

    level = _band(
        score,
        (
            (1, VerdictLevel.GOOD),
            (20, VerdictLevel.FAIR),
            (50, VerdictLevel.POOR),
            (float("inf"), VerdictLevel.BAD),
        ),
    )
    return Verdict(axis="structure", level=level, score=score, reasons=_reasons(relevant))


def build_verdicts(context: Context, findings: list[Finding]) -> dict[str, Verdict]:
    return {
        "originality": originality_verdict(context, findings),
        "privacy": privacy_verdict(context, findings),
        "structure": structure_verdict(context, findings),
    }
