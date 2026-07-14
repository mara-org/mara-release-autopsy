from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from mara_autopsy.analyzer import AnalysisConfig, analyze_releases
from mara_autopsy.models import DailyHealth, HealthDataset, ReleaseEvent


def _release(day: date, repository: str = "api", tag: str = "v1") -> ReleaseEvent:
    return ReleaseEvent(repository, tag, datetime.combine(day, datetime.min.time()))


def _dataset(
    start: date,
    end: date,
    *,
    values: dict[date, dict[str, float | None]] | None = None,
) -> HealthDataset:
    values = values or {}
    days = []
    current = start
    while current <= end:
        overrides = values.get(current, {})
        days.append(
            DailyHealth(
                day=current,
                sleep_minutes=overrides.get("sleep_minutes", 480.0),
                bedtime_minute=overrides.get("bedtime_minute", 23.0 * 60.0),
                steps=overrides.get("steps", 10_000.0),
                resting_heart_rate=overrides.get("resting_heart_rate", 60.0),
            )
        )
        current += timedelta(days=1)
    return HealthDataset(tuple(days), sources_by_metric={})


def _comparison(impact, metric: str):
    return next(item for item in impact.comparisons if item.metric == metric)


def test_no_releases_returns_an_empty_report_with_warning() -> None:
    health = _dataset(date(2025, 1, 1), date(2025, 1, 3))

    report = analyze_releases(health, [])

    assert report.releases_seen == 0
    assert report.impacts == ()
    assert any("No releases" in warning for warning in report.warnings)


def test_insufficient_data_is_reported_without_fabricating_comparisons() -> None:
    release_day = date(2025, 3, 1)
    health = _dataset(release_day - timedelta(days=2), release_day)

    report = analyze_releases(health, [_release(release_day)])

    assert report.impacts == ()
    assert any("Skipped api:v1" in warning for warning in report.warnings)
    assert any("no metric with sufficient data" in warning for warning in report.warnings)


def test_circular_bedtime_treats_half_past_midnight_as_later_than_2330() -> None:
    release_day = date(2025, 4, 30)
    start = release_day - timedelta(weeks=9)
    values: dict[date, dict[str, float | None]] = {}
    for offset in range(7):
        day = release_day - timedelta(days=offset)
        values[day] = {"bedtime_minute": 30.0}
    for week in range(1, 9):
        for offset in range(7):
            day = release_day - timedelta(days=offset, weeks=week)
            values[day] = {"bedtime_minute": 23.5 * 60.0}
    health = _dataset(start, release_day, values=values)

    report = analyze_releases(health, [_release(release_day)])

    bedtime = _comparison(report.impacts[0], "bedtime_minute")
    assert bedtime.window_value == pytest.approx(30.0)
    assert bedtime.baseline_value == pytest.approx(1410.0)
    assert bedtime.difference == pytest.approx(60.0)
    assert bedtime.percent_difference is None


def test_circular_bedtime_mean_wraps_to_midnight_not_noon_or_2400() -> None:
    release_day = date(2025, 5, 31)
    start = release_day - timedelta(weeks=9)
    values: dict[date, dict[str, float | None]] = {}
    around_midnight = (1410.0, 30.0, 1410.0, 30.0, 0.0, 0.0, 0.0)
    for offset, bedtime in enumerate(around_midnight):
        values[release_day - timedelta(days=offset)] = {"bedtime_minute": bedtime}
    health = _dataset(start, release_day, values=values)

    report = analyze_releases(health, [_release(release_day)])

    bedtime = _comparison(report.impacts[0], "bedtime_minute")
    assert bedtime.window_value == pytest.approx(0.0, abs=1e-9)


def test_other_release_windows_are_excluded_from_the_baseline() -> None:
    current_day = date(2025, 6, 30)
    earlier_day = current_day - timedelta(weeks=2)
    start = current_day - timedelta(weeks=10)
    values: dict[date, dict[str, float | None]] = {}

    # Poison every day in the earlier release window.  Those seven dates are
    # otherwise weekday-matched candidates for the current release baseline.
    for offset in range(7):
        values[earlier_day - timedelta(days=offset)] = {"sleep_minutes": 60.0}

    health = _dataset(start, current_day, values=values)
    releases = [_release(earlier_day, tag="old"), _release(current_day, tag="new")]

    report = analyze_releases(health, releases)
    current = next(impact for impact in report.impacts if impact.release.tag == "new")
    sleep = _comparison(current, "sleep_minutes")

    assert sleep.baseline_value == pytest.approx(480.0)
    assert sleep.baseline_samples == 49


def test_overlapping_release_windows_warn_that_attribution_is_not_possible() -> None:
    release_day = date(2025, 8, 31)
    health = _dataset(release_day - timedelta(weeks=10), release_day + timedelta(days=1))
    releases = [
        _release(release_day, "api", "v1"),
        _release(release_day + timedelta(days=1), "web", "v2"),
    ]

    report = analyze_releases(health, releases)

    assert all(any("overlaps" in warning for warning in item.warnings) for item in report.impacts)
    assert all(
        any("cannot be attributed" in warning for warning in item.warnings)
        for item in report.impacts
    )


def test_impact_score_is_bounded_emphasizes_sleep_and_sorts_deterministically() -> None:
    high_day = date(2025, 10, 31)
    low_day = date(2025, 8, 31)
    start = low_day - timedelta(weeks=9)
    values: dict[date, dict[str, float | None]] = {}

    for offset in range(7):
        values[high_day - timedelta(days=offset)] = {
            "sleep_minutes": 300.0,
            "bedtime_minute": 120.0,
            "steps": 1_000.0,
            "resting_heart_rate": 80.0,
        }
        values[low_day - timedelta(days=offset)] = {
            "sleep_minutes": 450.0,
            "bedtime_minute": 23.0 * 60.0,
            "steps": 10_000.0,
            "resting_heart_rate": 60.0,
        }

    health = _dataset(start, high_day, values=values)
    releases = [
        _release(low_day, "z-repo", "v2"),
        _release(high_day, "a-repo", "v3"),
        _release(low_day, "a-repo", "v1"),
    ]

    report = analyze_releases(health, releases)

    assert report.impacts[0].release.tag == "v3"
    assert report.impacts[0].impact_score == 100.0
    assert [item.release.repository for item in report.impacts[1:]] == ["a-repo", "z-repo"]
    assert all(0.0 <= item.impact_score <= 100.0 for item in report.impacts)


def test_zero_baseline_yields_no_percent_and_no_division_error() -> None:
    release_day = date(2025, 12, 31)
    start = release_day - timedelta(weeks=9)
    values: dict[date, dict[str, float | None]] = {}
    for day_offset in range(7):
        values[release_day - timedelta(days=day_offset)] = {"steps": 500.0}
    for week in range(1, 9):
        for day_offset in range(7):
            values[release_day - timedelta(days=day_offset, weeks=week)] = {"steps": 0.0}
    health = _dataset(start, release_day, values=values)

    report = analyze_releases(health, [_release(release_day)])

    steps = _comparison(report.impacts[0], "steps")
    assert steps.baseline_value == 0.0
    assert steps.percent_difference is None
    assert report.impacts[0].impact_score == 0.0


def test_config_rejects_non_positive_values() -> None:
    with pytest.raises(ValueError, match="window_days"):
        AnalysisConfig(window_days=0)


def test_skipped_release_details_are_bounded() -> None:
    health = _dataset(date(2025, 1, 1), date(2025, 1, 2))
    releases = [
        _release(date(2026, 1, 1) + timedelta(days=offset), tag=f"v{offset}")
        for offset in range(25)
    ]

    report = analyze_releases(health, releases)

    detailed = [warning for warning in report.warnings if warning.startswith("Skipped ")]
    assert report.impacts == ()
    assert len(detailed) == 20
    assert any("5 additional skipped" in warning for warning in report.warnings)
