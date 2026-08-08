"""Rule registry.

Rules are plain generator functions that inspect a :class:`Context` and yield
:class:`Finding` objects. Registration is a decorator so a rule pack is just a
module that gets imported — adding a rule never means editing the engine.

A rule that raises is caught and reported as an internal error rather than
killing the whole analysis. One buggy heuristic must not cost the user their
report.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from ..models import Category, Confidence, Finding, Severity
from .context import Context

RuleFunc = Callable[[Context], Iterable[Finding]]


@dataclass(frozen=True)
class RuleSpec:
    """A registered rule and the metadata the engine needs to run it."""

    name: str
    category: Category
    func: RuleFunc
    order: int = 100


_RULES: list[RuleSpec] = []


def rule(name: str, category: Category, order: int = 100) -> Callable[[RuleFunc], RuleFunc]:
    """Register an analysis rule."""

    def decorator(func: RuleFunc) -> RuleFunc:
        _RULES.append(RuleSpec(name=name, category=category, func=func, order=order))
        return func

    return decorator


def all_rules() -> list[RuleSpec]:
    return sorted(_RULES, key=lambda r: (r.order, r.name))


def run_rules(context: Context) -> Iterator[Finding]:
    """Run every registered rule, isolating failures."""
    for spec in all_rules():
        try:
            yield from spec.func(context)
        except Exception as exc:  # noqa: BLE001 - deliberate isolation boundary
            yield Finding(
                id="internal.rule_failed",
                category=spec.category,
                severity=Severity.NOTICE,
                confidence=Confidence.HIGH,
                params={"name": spec.name, "error": f"{type(exc).__name__}: {exc}"},
                evidence={"traceback": traceback.format_exc(limit=3)},
            )
