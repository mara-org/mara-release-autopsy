from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from mara_autopsy.models import (
    AutopsyReport,
    MetricComparison,
    ReleaseEvent,
    ReleaseImpact,
)
from mara_autopsy.reporters import (
    DISCLAIMER,
    render,
    render_json,
    render_markdown,
    render_terminal,
    render_text,
)


def sample_report() -> AutopsyReport:
    release = ReleaseEvent("payments-api", "v2.4.0", datetime(2026, 4, 8, tzinfo=timezone.utc))
    impact = ReleaseImpact(
        release=release,
        window_start=date(2026, 4, 2),
        window_end=date(2026, 4, 8),
        comparisons=(
            MetricComparison("sleep_minutes", "minutes", 370, 438, -68, -15.525, 7, 42),
            MetricComparison("bedtime_minute", "minute_of_day", 94, 0, 94, None, 7, 42),
            MetricComparison("steps", "count", 5200, 7500, -2300, -30.667, 7, 42),
            MetricComparison("resting_heart_rate", "bpm", 62.3, 59.1, 3.2, 5.414, 6, 35),
        ),
        impact_score=78.4,
        warnings=("Only six resting-heart-rate days were available.",),
    )
    return AutopsyReport(
        health_start=date(2025, 4, 1),
        health_end=date(2026, 4, 10),
        releases_seen=3,
        impacts=(impact,),
        warnings=("Two releases had insufficient data.",),
    )


def test_text_report_is_compact_and_keeps_safety_boundary() -> None:
    result = render_text(sample_report())

    assert "payments-api  v2.4.0" in result
    assert "Sleep        6h 10m vs 7h 18m (-1h 08m/night; -15.5%)" in result
    assert "Bedtime      01:34 vs 00:00 (+1h 34m later)" in result
    assert "Observed sleep difference across days with data: -7h 56m" in result
    assert DISCLAIMER in result
    assert "\x1b" not in result


def test_markdown_report_has_table_and_disclaimer() -> None:
    result = render_markdown(sample_report())

    assert "| Metric | Release window vs baseline |" in result
    assert "`payments-api` · `v2.4.0`" in result
    assert f"> {DISCLAIMER}" in result


def test_markdown_report_uses_a_safe_fence_for_repository_content() -> None:
    report = sample_report()
    unsafe_release = ReleaseEvent(
        "repo`name", "v1`preview", datetime(2026, 4, 8, tzinfo=timezone.utc)
    )
    unsafe_impact = ReleaseImpact(
        release=unsafe_release,
        window_start=report.impacts[0].window_start,
        window_end=report.impacts[0].window_end,
        comparisons=report.impacts[0].comparisons,
        impact_score=report.impacts[0].impact_score,
    )
    unsafe_report = AutopsyReport(report.health_start, report.health_end, 1, (unsafe_impact,))

    result = render_markdown(unsafe_report)

    assert "``repo`name`` · ``v1`preview``" in result


def test_json_report_is_versioned_and_machine_readable() -> None:
    payload = json.loads(render_json(sample_report()))

    assert payload["schema_version"] == 1
    assert payload["impacts"][0]["repository"] == "payments-api"
    assert payload["impacts"][0]["comparisons"][0]["difference"] == -68
    assert payload["disclaimer"] == DISCLAIMER


def test_empty_report_explains_that_data_was_insufficient() -> None:
    report = AutopsyReport(None, None, 0, ())

    result = render_text(report)

    assert "No release window had enough matched health data" in result
    assert "0 analyzable / 0 found" in result


def test_terminal_report_is_video_friendly_and_honest() -> None:
    result = render_terminal(sample_report())

    assert "MARA // RELEASE AUTOPSY" in result
    assert "your body kept the receipts" in result
    assert "███████████████████░░░░░" in result
    assert "The tag shipped. Your bedtime requested a rollback." in result
    assert "Interesting timing is not proof. This is not medical advice." in result
    assert "\x1b" not in result


def test_terminal_report_color_is_opt_in() -> None:
    result = render_terminal(sample_report(), color=True)

    assert "\x1b[" in result
    assert "payments-api" in result


def test_terminal_report_neutralizes_terminal_control_characters() -> None:
    report = sample_report()
    unsafe_release = ReleaseEvent(
        "repo\x1b[31m\nname", "v1\rpreview", datetime(2026, 4, 8, tzinfo=timezone.utc)
    )
    unsafe_impact = ReleaseImpact(
        release=unsafe_release,
        window_start=report.impacts[0].window_start,
        window_end=report.impacts[0].window_end,
        comparisons=report.impacts[0].comparisons,
        impact_score=report.impacts[0].impact_score,
    )

    result = render_terminal(
        AutopsyReport(report.health_start, report.health_end, 1, (unsafe_impact,))
    )

    assert "repo [31m name v1 preview" in result
    assert "\x1b" not in result


def test_empty_terminal_report_refuses_to_invent_drama() -> None:
    result = render_terminal(AutopsyReport(None, None, 0, ()))

    assert "NO RECEIPT" in result
    assert "refuses to invent drama" in result


def test_render_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="unsupported output format"):
        render(sample_report(), "html")
