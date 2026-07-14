"""Human and machine-readable report rendering."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from mara_autopsy.models import AutopsyReport, MetricComparison, ReleaseImpact

DISCLAIMER = (
    "Observational comparison only. Release timing may coincide with other changes; "
    "this report does not establish causation or provide medical advice. The score ranks "
    "windows inside this export; it is not a health score."
)

_BACKTICK_RUN = re.compile(r"`+")
_TERMINAL_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"


def _clock(minutes: float) -> str:
    rounded = round(minutes) % (24 * 60)
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def _duration(minutes: float, *, signed: bool = False) -> str:
    sign = ""
    if signed:
        sign = "+" if minutes > 0 else "-" if minutes < 0 else ""
    rounded = abs(round(minutes))
    hours, remainder = divmod(rounded, 60)
    if hours and remainder:
        return f"{sign}{hours}h {remainder:02d}m"
    if hours:
        return f"{sign}{hours}h"
    return f"{sign}{remainder}m"


def _percentage(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:+.1f}%"


def _markdown_code(value: str) -> str:
    longest_run = max((len(match.group()) for match in _BACKTICK_RUN.finditer(value)), default=0)
    fence = "`" * (longest_run + 1)
    return f"{fence}{value}{fence}"


def _comparison_text(comparison: MetricComparison) -> str:
    metric = comparison.metric
    if metric == "sleep_minutes":
        return (
            f"{_duration(comparison.window_value)} vs "
            f"{_duration(comparison.baseline_value)} "
            f"({_duration(comparison.difference, signed=True)}/night; "
            f"{_percentage(comparison.percent_difference)})"
        )
    if metric == "bedtime_minute":
        direction = "later" if comparison.difference > 0 else "earlier"
        if comparison.difference == 0:
            direction = "change"
        return (
            f"{_clock(comparison.window_value)} vs {_clock(comparison.baseline_value)} "
            f"({_duration(comparison.difference, signed=True)} {direction})"
        )
    if metric == "steps":
        return (
            f"{comparison.window_value:,.0f} vs {comparison.baseline_value:,.0f} "
            f"({comparison.difference:+,.0f}/day; "
            f"{_percentage(comparison.percent_difference)})"
        )
    if metric == "resting_heart_rate":
        return (
            f"{comparison.window_value:.1f} vs {comparison.baseline_value:.1f} bpm "
            f"({comparison.difference:+.1f}; {_percentage(comparison.percent_difference)})"
        )
    return (
        f"{comparison.window_value:.2f} vs {comparison.baseline_value:.2f} "
        f"({comparison.difference:+.2f}; {_percentage(comparison.percent_difference)})"
    )


def _metric_label(metric: str) -> str:
    return {
        "sleep_minutes": "Sleep",
        "bedtime_minute": "Bedtime",
        "steps": "Steps",
        "resting_heart_rate": "Resting HR",
    }.get(metric, metric.replace("_", " ").title())


def _paint(value: str, *codes: str, enabled: bool) -> str:
    if not enabled:
        return value
    return f"{''.join(codes)}{value}{_RESET}"


def _terminal_field(value: str, *, limit: int = 72) -> str:
    """Keep terminal-facing repository data on one harmless, bounded line."""

    clean = _TERMINAL_UNSAFE.sub(" ", value)
    clean = " ".join(clean.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _receipt_level(score: float) -> tuple[str, str]:
    if score >= 75:
        return "VERY LOUD", _RED
    if score >= 50:
        return "LOUD", _YELLOW
    if score >= 25:
        return "NOTED", _CYAN
    return "QUIET", _GREEN


def _score_bar(score: float, *, width: int = 24) -> str:
    bounded = min(max(score, 0.0), 100.0)
    filled = round(bounded / 100.0 * width)
    return "█" * filled + "░" * (width - filled)


def _terminal_metric(comparison: MetricComparison) -> tuple[str, str, str, str]:
    metric = comparison.metric
    if metric == "sleep_minutes":
        change = "— no change"
        if comparison.difference < 0:
            change = f"↓ {_duration(comparison.difference)}/night"
        elif comparison.difference > 0:
            change = f"↑ {_duration(comparison.difference)}/night"
        return (
            "SLEEP",
            _duration(comparison.window_value),
            _duration(comparison.baseline_value),
            change,
        )
    if metric == "bedtime_minute":
        change = "— no change"
        if comparison.difference > 0:
            change = f"→ {_duration(comparison.difference)} later"
        elif comparison.difference < 0:
            change = f"← {_duration(comparison.difference)} earlier"
        return "BEDTIME", _clock(comparison.window_value), _clock(comparison.baseline_value), change
    if metric == "steps":
        change = "— no change"
        if comparison.percent_difference is not None:
            arrow = "↓" if comparison.percent_difference < 0 else "↑"
            if comparison.percent_difference == 0:
                arrow = "—"
            change = f"{arrow} {abs(comparison.percent_difference):.1f}%"
        return (
            "STEPS",
            f"{comparison.window_value:,.0f}",
            f"{comparison.baseline_value:,.0f}",
            change,
        )
    if metric == "resting_heart_rate":
        arrow = "↓" if comparison.difference < 0 else "↑"
        if comparison.difference == 0:
            arrow = "—"
        return (
            "RESTING HR",
            f"{comparison.window_value:.1f} bpm",
            f"{comparison.baseline_value:.1f} bpm",
            f"{arrow} {abs(comparison.difference):.1f} bpm",
        )
    return (
        _metric_label(metric).upper(),
        f"{comparison.window_value:.2f}",
        f"{comparison.baseline_value:.2f}",
        f"{comparison.difference:+.2f}",
    )


def _punchline(impact: ReleaseImpact) -> str:
    comparisons = {item.metric: item for item in impact.comparisons}
    sleep = comparisons.get("sleep_minutes")
    bedtime = comparisons.get("bedtime_minute")
    steps = comparisons.get("steps")
    resting_hr = comparisons.get("resting_heart_rate")

    if (
        sleep is not None
        and sleep.difference <= -45
        and bedtime is not None
        and bedtime.difference >= 60
    ):
        return "The tag shipped. Your bedtime requested a rollback."
    if sleep is not None and sleep.difference <= -30:
        return "The tag shipped. Sleep stayed in draft."
    if bedtime is not None and bedtime.difference >= 45:
        return "Production went live. Bedtime missed the deployment window."
    if (
        steps is not None
        and steps.percent_difference is not None
        and steps.percent_difference <= -20
    ):
        return "The code moved more than the step counter."
    if resting_hr is not None and resting_hr.difference >= 3:
        return "The release looked calm. Resting heart rate left a review."
    if impact.impact_score < 10:
        return "Clean ship. Your routine barely opened an issue."
    return "Interesting timing. Coincidence has entered the chat."


def render_terminal(
    report: AutopsyReport,
    *,
    color: bool = False,
    ranking_limit: int = 5,
) -> str:
    """Render a video-friendly terminal reveal with a bounded ranking."""

    heading = _paint(f"{'MARA // RELEASE AUTOPSY':<58}", _BOLD, _CYAN, enabled=color)
    tagline = _paint(f"{'your body kept the receipts':<58}", _DIM, enabled=color)
    lines = [
        "╭──────────────────────────────────────────────────────────────╮",
        f"│  {heading}  │",
        f"│  {tagline}  │",
        "╰──────────────────────────────────────────────────────────────╯",
        "",
    ]

    if not report.impacts:
        lines.extend(
            [
                _paint("NO RECEIPT", _BOLD, _GREEN, enabled=color),
                "Not enough matched data. The tool refuses to invent drama.",
                "",
                _paint(
                    "Interesting timing is not proof. This is not medical advice.",
                    _DIM,
                    enabled=color,
                ),
                "",
            ]
        )
        return "\n".join(lines)

    impact = report.impacts[0]
    release = impact.release
    level, level_color = _receipt_level(impact.impact_score)
    release_name = _terminal_field(f"{release.repository}  {release.tag}")
    lines.extend(
        [
            _paint("THE LOUDEST RELEASE WINDOW", _BOLD, enabled=color),
            f"{_paint(release_name, _BOLD, enabled=color)}  "
            f"{_paint(release.day.isoformat(), _DIM, enabled=color)}",
            "",
            f"{_paint(_score_bar(impact.impact_score), level_color, enabled=color)}  "
            f"{impact.impact_score:.0f}/100  "
            f"{_paint(level, _BOLD, level_color, enabled=color)}",
            _paint(
                "Receipt score ranks unusual timing. It is not a health score.", _DIM, enabled=color
            ),
            "",
        ]
    )

    for comparison in impact.comparisons:
        label, current, baseline, change = _terminal_metric(comparison)
        lines.append(
            f"  {_paint(f'{label:<20}', _BOLD, enabled=color)}"
            f"{current:<13}"
            f"{_paint('usual', _DIM, enabled=color)} {baseline:<12}"
            f"{change}"
        )

    sleep = next((item for item in impact.comparisons if item.metric == "sleep_minutes"), None)
    lines.extend(["", _paint("THE RECEIPT", _BOLD, _CYAN, enabled=color)])
    if sleep is not None:
        total = sleep.difference * sleep.window_samples
        direction = "less" if total < 0 else "more"
        lines.append(
            f"{_duration(total)} {direction} sleep across {sleep.window_samples} observed nights."
        )
    lines.append(_paint(f"“{_punchline(impact)}”", _BOLD, enabled=color))

    if impact.warnings:
        caveat = (
            "1 caveat applies"
            if len(impact.warnings) == 1
            else (f"{len(impact.warnings)} caveats apply")
        )
        lines.extend(
            [
                "",
                _paint(
                    f"DATA NOTE  {caveat}; use --plain for details.",
                    _YELLOW,
                    enabled=color,
                ),
            ]
        )

    ranked = report.impacts[1 : max(1, ranking_limit)]
    if ranked:
        lines.extend(["", _paint("OTHER RECEIPTS", _BOLD, enabled=color)])
        for position, other in enumerate(ranked, start=2):
            other_level, other_color = _receipt_level(other.impact_score)
            label = _terminal_field(
                f"{position}. {other.release.repository}  {other.release.tag}", limit=48
            )
            score_label = f"{other.impact_score:.0f}/100 {other_level}"
            lines.append(f"{label:<50} {_paint(score_label, other_color, enabled=color)}")

    hidden = len(report.impacts) - 1 - len(ranked)
    if hidden > 0:
        lines.append(_paint(f"+ {hidden} more in --plain, Markdown, or JSON.", _DIM, enabled=color))

    lines.extend(
        [
            "",
            _paint(
                "Interesting timing is not proof. This is not medical advice.", _DIM, enabled=color
            ),
            "",
        ]
    )
    return "\n".join(lines)


def render_text(report: AutopsyReport) -> str:
    """Render a compact terminal report without ANSI control sequences."""

    period = "no usable health dates"
    if report.health_start and report.health_end:
        period = f"{report.health_start.isoformat()} to {report.health_end.isoformat()}"

    lines = [
        "MARA RELEASE AUTOPSY",
        f"Health history: {period}",
        f"Release tags: {len(report.impacts)} analyzable / {report.releases_seen} found",
        "",
    ]

    if not report.impacts:
        lines.extend(
            [
                "No release window had enough matched health data for a defensible comparison.",
                "",
            ]
        )

    for position, impact in enumerate(report.impacts, start=1):
        release = impact.release
        lines.extend(
            [
                f"#{position}  {release.repository}  {release.tag}",
                f"Released: {release.day.isoformat()}",
                "Compared window: "
                f"{impact.window_start.isoformat()} to {impact.window_end.isoformat()}",
                f"Impact score: {impact.impact_score:.0f}/100",
            ]
        )
        for comparison in impact.comparisons:
            label = _metric_label(comparison.metric)
            lines.append(f"  {label:<12} {_comparison_text(comparison)}")

        sleep = next((item for item in impact.comparisons if item.metric == "sleep_minutes"), None)
        if sleep:
            total = sleep.difference * sleep.window_samples
            lines.append(
                "  Observed sleep difference across days with data: "
                f"{_duration(total, signed=True)}"
            )
        for warning in impact.warnings:
            lines.append(f"  Note: {warning}")
        lines.append("")

    if report.warnings:
        lines.append("Notes:")
        lines.extend(f"- {warning}" for warning in report.warnings)
        lines.append("")

    lines.extend([DISCLAIMER, ""])
    return "\n".join(lines)


def render_markdown(report: AutopsyReport) -> str:
    """Render a shareable Markdown report."""

    period = "No usable health dates"
    if report.health_start and report.health_end:
        period = f"{report.health_start.isoformat()} to {report.health_end.isoformat()}"
    lines = [
        "# Mara Release Autopsy",
        "",
        f"**Health history:** {period}  ",
        f"**Release tags:** {len(report.impacts)} analyzable / {report.releases_seen} found",
        "",
    ]
    if not report.impacts:
        lines.extend(
            [
                "No release window had enough matched health data for a defensible comparison.",
                "",
            ]
        )
    for position, impact in enumerate(report.impacts, start=1):
        release = impact.release
        lines.extend(
            [
                f"## {position}. {_markdown_code(release.repository)} · "
                f"{_markdown_code(release.tag)}",
                "",
                f"Released {release.day.isoformat()} · "
                f"Impact score **{impact.impact_score:.0f}/100**",
                "",
                "| Metric | Release window vs baseline |",
                "| --- | ---: |",
            ]
        )
        for comparison in impact.comparisons:
            lines.append(f"| {_metric_label(comparison.metric)} | {_comparison_text(comparison)} |")
        lines.append("")
        for warning in impact.warnings:
            lines.append(f"> Note: {warning}")
        if impact.warnings:
            lines.append("")
    if report.warnings:
        lines.extend(["## Notes", ""])
        lines.extend(f"- {warning}" for warning in report.warnings)
        lines.append("")
    lines.extend([f"> {DISCLAIMER}", ""])
    return "\n".join(lines)


def _comparison_dict(comparison: MetricComparison) -> dict[str, Any]:
    return {
        "metric": comparison.metric,
        "unit": comparison.unit,
        "window_value": comparison.window_value,
        "baseline_value": comparison.baseline_value,
        "difference": comparison.difference,
        "percent_difference": comparison.percent_difference,
        "window_samples": comparison.window_samples,
        "baseline_samples": comparison.baseline_samples,
    }


def _impact_dict(impact: ReleaseImpact) -> dict[str, Any]:
    return {
        "repository": impact.release.repository,
        "tag": impact.release.tag,
        "released_at": impact.release.released_at.isoformat(),
        "window_start": impact.window_start.isoformat(),
        "window_end": impact.window_end.isoformat(),
        "impact_score": impact.impact_score,
        "comparisons": [_comparison_dict(item) for item in impact.comparisons],
        "warnings": list(impact.warnings),
    }


def render_json(report: AutopsyReport) -> str:
    """Render a versioned JSON document for scripts and future UI clients."""

    payload = {
        "schema_version": 1,
        "disclaimer": DISCLAIMER,
        "health_start": report.health_start.isoformat() if report.health_start else None,
        "health_end": report.health_end.isoformat() if report.health_end else None,
        "releases_seen": report.releases_seen,
        "impacts": [_impact_dict(item) for item in report.impacts],
        "warnings": list(report.warnings),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render(report: AutopsyReport, output_format: str) -> str:
    """Dispatch to a named reporter."""

    if output_format == "text":
        return render_text(report)
    if output_format == "markdown":
        return render_markdown(report)
    if output_format == "json":
        return render_json(report)
    raise ValueError(f"unsupported output format: {output_format}")
