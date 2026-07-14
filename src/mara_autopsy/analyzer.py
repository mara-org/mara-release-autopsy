"""Deterministic, non-causal comparisons around local Git releases.

The impact score is deliberately simple and bounded.  Only adverse changes add
points: up to 50 for an hour of lost sleep, 20 for a two-hour later bedtime,
15 for a 50 percent step reduction, and 15 for a 10 bpm resting-heart-rate
increase.  Missing metrics are not imputed and the score is not reweighted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from math import atan2, cos, hypot, isfinite, pi, sin
from statistics import fmean

from mara_autopsy.models import (
    AutopsyReport,
    DailyHealth,
    HealthDataset,
    MetricComparison,
    ReleaseEvent,
    ReleaseImpact,
)


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    """Calendar windows and evidence thresholds used by :func:`analyze_releases`.

    ``window_days`` is the total number of calendar dates in the window and
    includes the release date.
    """

    window_days: int = 7
    baseline_weeks: int = 8
    min_window_samples: int = 4
    min_baseline_samples: int = 12

    def __post_init__(self) -> None:
        for name in (
            "window_days",
            "baseline_weeks",
            "min_window_samples",
            "min_baseline_samples",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


_DEFAULT_CONFIG = AnalysisConfig()
_MAX_SKIPPED_RELEASE_WARNINGS = 20


@dataclass(frozen=True, slots=True)
class _MetricSpec:
    name: str
    unit: str
    getter: Callable[[DailyHealth], float | None]
    circular: bool = False


_METRICS = (
    _MetricSpec("sleep_minutes", "minutes", lambda day: day.sleep_minutes),
    _MetricSpec("bedtime_minute", "minute_of_day", lambda day: day.bedtime_minute, True),
    _MetricSpec("steps", "count", lambda day: day.steps),
    _MetricSpec(
        "resting_heart_rate",
        "bpm",
        lambda day: day.resting_heart_rate,
    ),
)


def analyze_releases(
    health: HealthDataset,
    releases: Iterable[ReleaseEvent],
    config: AnalysisConfig = _DEFAULT_CONFIG,
) -> AutopsyReport:
    """Compare release windows with prior weekday-matched observations.

    A release window ends on the release date.  Its baseline contains the same
    weekdays from the configured number of prior weeks.  Dates belonging to any
    release window are excluded from every baseline so nearby releases cannot
    quietly contaminate the comparison.  Results are associations only.
    """

    release_items = tuple(releases)
    health_start = health.days[0].day if health.days else None
    health_end = health.days[-1].day if health.days else None
    report_warnings = list(health.warnings)

    if not release_items:
        report_warnings.append("No releases were supplied; no release windows were analyzed.")
        return AutopsyReport(
            health_start=health_start,
            health_end=health_end,
            releases_seen=0,
            impacts=(),
            warnings=tuple(report_warnings),
        )

    if not health.days:
        report_warnings.append("No health days are available for release-window analysis.")

    days_by_date = {item.day: item for item in health.days}
    if len(days_by_date) != len(health.days):
        report_warnings.append(
            "Duplicate health dates were present; the last observation for each date was used."
        )

    windows = tuple(
        (
            release,
            release.day - timedelta(days=config.window_days - 1),
            release.day,
        )
        for release in release_items
    )
    all_release_dates = {
        day
        for _, window_start, window_end in windows
        for day in _date_range(window_start, window_end)
    }

    candidates = [
        _analyze_release(
            release=release,
            window_start=window_start,
            window_end=window_end,
            health_start=health_start,
            health_end=health_end,
            days_by_date=days_by_date,
            all_windows=windows,
            excluded_baseline_dates=all_release_dates,
            config=config,
        )
        for release, window_start, window_end in windows
    ]
    skipped = [impact for impact in candidates if not impact.comparisons]
    impacts = [impact for impact in candidates if impact.comparisons]
    impacts.sort(
        key=lambda impact: (
            -impact.impact_score,
            impact.release.day,
            impact.release.repository,
            impact.release.tag,
        )
    )

    if skipped:
        detailed_skips = sorted(
            skipped,
            key=lambda impact: (
                impact.release.day,
                impact.release.repository,
                impact.release.tag,
            ),
            reverse=True,
        )[:_MAX_SKIPPED_RELEASE_WARNINGS]
        for impact in detailed_skips:
            coverage_warning = next(
                (
                    warning
                    for warning in impact.warnings
                    if "outside health coverage" in warning
                    or "No health coverage" in warning
                    or "Health rows cover" in warning
                ),
                "No metric met the configured sample thresholds.",
            )
            report_warnings.append(
                f"Skipped {impact.release.repository}:{impact.release.tag} "
                f"({impact.release.day.isoformat()}): {coverage_warning}"
            )
        omitted = len(skipped) - len(detailed_skips)
        if omitted:
            report_warnings.append(
                f"Omitted per-release details for {omitted} additional skipped windows."
            )
        report_warnings.append(
            f"{len(skipped)} of {len(candidates)} release windows had no metric with "
            "sufficient data."
        )
    report_warnings.append(
        "Release-window differences are temporal associations and do not establish causality."
    )

    return AutopsyReport(
        health_start=health_start,
        health_end=health_end,
        releases_seen=len(release_items),
        impacts=tuple(impacts),
        warnings=tuple(report_warnings),
    )


def _analyze_release(
    *,
    release: ReleaseEvent,
    window_start: date,
    window_end: date,
    health_start: date | None,
    health_end: date | None,
    days_by_date: dict[date, DailyHealth],
    all_windows: Sequence[tuple[ReleaseEvent, date, date]],
    excluded_baseline_dates: set[date],
    config: AnalysisConfig,
) -> ReleaseImpact:
    warnings: list[str] = []
    window_dates = tuple(_date_range(window_start, window_end))
    window_days = tuple(days_by_date[day] for day in window_dates if day in days_by_date)

    if health_start is None or health_end is None:
        warnings.append("No health coverage is available for this release window.")
    elif release.day < health_start or release.day > health_end:
        warnings.append(
            f"Release date {release.day.isoformat()} is outside health coverage "
            f"{health_start.isoformat()} through {health_end.isoformat()}."
        )

    if len(window_days) < len(window_dates):
        warnings.append(
            f"Health rows cover {len(window_days)} of {len(window_dates)} release-window dates."
        )

    overlapping = sorted(
        (
            other
            for other, other_start, other_end in all_windows
            if other is not release and max(window_start, other_start) <= min(window_end, other_end)
        ),
        key=lambda item: (item.day, item.repository, item.tag),
    )
    if overlapping:
        labels = ", ".join(
            f"{item.repository}:{item.tag} ({item.day.isoformat()})" for item in overlapping
        )
        warnings.append(
            "This release window overlaps other release windows; effects cannot be attributed "
            f"to one release ({labels})."
        )

    baseline_dates = sorted(
        {
            window_day - timedelta(weeks=week)
            for window_day in window_dates
            for week in range(1, config.baseline_weeks + 1)
        }
        - excluded_baseline_dates
    )
    baseline_days = tuple(days_by_date[day] for day in baseline_dates if day in days_by_date)

    comparisons: list[MetricComparison] = []
    for metric in _METRICS:
        window_values = _values(window_days, metric)
        baseline_values = _values(baseline_days, metric)
        if len(window_values) < config.min_window_samples:
            warnings.append(
                f"{metric.name}: {len(window_values)} release-window samples; "
                f"requires {config.min_window_samples}."
            )
            continue
        if len(baseline_values) < config.min_baseline_samples:
            warnings.append(
                f"{metric.name}: {len(baseline_values)} uncontaminated weekday-matched "
                f"baseline samples; requires {config.min_baseline_samples}."
            )
            continue

        comparison = _comparison(metric, window_values, baseline_values)
        if comparison is None:
            warnings.append(
                f"{metric.name}: circular mean is undefined for the available observations."
            )
            continue
        comparisons.append(comparison)

    if not comparisons:
        warnings.append("No metric had enough comparable evidence to calculate an impact.")

    return ReleaseImpact(
        release=release,
        window_start=window_start,
        window_end=window_end,
        comparisons=tuple(comparisons),
        impact_score=_impact_score(comparisons),
        warnings=tuple(warnings),
    )


def _date_range(start: date, end: date) -> Iterable[date]:
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def _values(days: Sequence[DailyHealth], metric: _MetricSpec) -> tuple[float, ...]:
    values: list[float] = []
    for day in days:
        value = metric.getter(day)
        if value is None:
            continue
        number = float(value)
        if not isfinite(number):
            continue
        values.append(number % 1440.0 if metric.circular else number)
    return tuple(values)


def _comparison(
    metric: _MetricSpec,
    window_values: Sequence[float],
    baseline_values: Sequence[float],
) -> MetricComparison | None:
    if metric.circular:
        window_value = _circular_mean(window_values)
        baseline_value = _circular_mean(baseline_values)
        if window_value is None or baseline_value is None:
            return None
        difference = _circular_difference(window_value, baseline_value)
        percent_difference = None
    else:
        window_value = fmean(window_values)
        baseline_value = fmean(baseline_values)
        difference = window_value - baseline_value
        percent_difference = difference / baseline_value * 100.0 if baseline_value != 0.0 else None

    return MetricComparison(
        metric=metric.name,
        unit=metric.unit,
        window_value=window_value,
        baseline_value=baseline_value,
        difference=difference,
        percent_difference=percent_difference,
        window_samples=len(window_values),
        baseline_samples=len(baseline_values),
    )


def _circular_mean(values: Sequence[float]) -> float | None:
    radians = tuple(value / 1440.0 * 2.0 * pi for value in values)
    mean_sin = fmean(sin(value) for value in radians)
    mean_cos = fmean(cos(value) for value in radians)
    if hypot(mean_sin, mean_cos) < 1e-12:
        return None
    angle = atan2(mean_sin, mean_cos)
    result = (angle % (2.0 * pi)) / (2.0 * pi) * 1440.0
    # Floating-point cancellation around midnight can otherwise render 24:00.
    return 0.0 if abs(result - 1440.0) < 1e-9 else result


def _circular_difference(window_value: float, baseline_value: float) -> float:
    """Return signed minutes in [-720, 720), where positive means later."""

    return (window_value - baseline_value + 720.0) % 1440.0 - 720.0


def _impact_score(comparisons: Sequence[MetricComparison]) -> float:
    by_metric = {comparison.metric: comparison for comparison in comparisons}
    score = 0.0

    sleep = by_metric.get("sleep_minutes")
    if sleep is not None:
        lost_minutes = max(0.0, -sleep.difference)
        score += 50.0 * min(lost_minutes / 60.0, 1.0)

    bedtime = by_metric.get("bedtime_minute")
    if bedtime is not None:
        later_minutes = max(0.0, bedtime.difference)
        score += 20.0 * min(later_minutes / 120.0, 1.0)

    steps = by_metric.get("steps")
    if steps is not None and steps.baseline_value > 0.0:
        lost_fraction = max(0.0, -steps.difference / steps.baseline_value)
        score += 15.0 * min(lost_fraction / 0.5, 1.0)

    resting_hr = by_metric.get("resting_heart_rate")
    if resting_hr is not None:
        increase = max(0.0, resting_hr.difference)
        score += 15.0 * min(increase / 10.0, 1.0)

    return round(min(max(score, 0.0), 100.0), 2)
