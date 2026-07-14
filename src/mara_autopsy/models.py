"""Small immutable values shared by the parser, Git reader, and analyzer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType


def _frozen_mapping(
    value: Mapping[str, tuple[str, ...]] | None = None,
) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class DailyHealth:
    """Normalized daily values; missing observations remain ``None``."""

    day: date
    sleep_minutes: float | None = None
    bedtime_minute: float | None = None
    steps: float | None = None
    resting_heart_rate: float | None = None


@dataclass(frozen=True, slots=True)
class HealthDataset:
    """A normalized, chronological Apple Health dataset."""

    days: tuple[DailyHealth, ...]
    records_seen: int = 0
    records_used: int = 0
    sources_by_metric: Mapping[str, tuple[str, ...]] = field(default_factory=_frozen_mapping)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources_by_metric", _frozen_mapping(self.sources_by_metric))
        if tuple(sorted(self.days, key=lambda item: item.day)) != self.days:
            raise ValueError("health days must be sorted chronologically")


@dataclass(frozen=True, slots=True)
class ReleaseEvent:
    """A local Git tag and the time Git assigns to it."""

    repository: str
    tag: str
    released_at: datetime

    @property
    def day(self) -> date:
        return self.released_at.date()


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """A release-window value compared with a matched historical baseline."""

    metric: str
    unit: str
    window_value: float
    baseline_value: float
    difference: float
    percent_difference: float | None
    window_samples: int
    baseline_samples: int


@dataclass(frozen=True, slots=True)
class ReleaseImpact:
    """One release window and every defensible comparison available for it."""

    release: ReleaseEvent
    window_start: date
    window_end: date
    comparisons: tuple[MetricComparison, ...]
    impact_score: float
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutopsyReport:
    """Complete deterministic analysis result."""

    health_start: date | None
    health_end: date | None
    releases_seen: int
    impacts: tuple[ReleaseImpact, ...]
    warnings: tuple[str, ...] = ()
