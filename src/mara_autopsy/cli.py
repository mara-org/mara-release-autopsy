"""Command-line entry point for Mara Release Autopsy."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import date
from pathlib import Path

from mara_autopsy import __version__
from mara_autopsy.analyzer import AnalysisConfig, analyze_releases
from mara_autopsy.apple_health import AppleHealthError, load_apple_health
from mara_autopsy.git_reader import GitReadError, discover_repositories, load_releases
from mara_autopsy.reporters import render, render_terminal


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mara-autopsy",
        description=(
            "Compare Apple Health history with local Git release tags. "
            "Runs locally and makes observational, non-medical comparisons."
        ),
    )
    parser.add_argument("health_export", type=Path, help="Apple Health export ZIP or export.xml")
    parser.add_argument(
        "repositories",
        nargs="+",
        type=Path,
        help="Git repository or parent directory containing repositories",
    )
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="report format (default: text)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the report atomically to this file instead of stdout",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="pace a video-friendly terminal reveal (text output only)",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="use the complete plain-text report instead of the terminal reveal",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable terminal colors (also respects NO_COLOR)",
    )
    parser.add_argument(
        "--since",
        type=_iso_date,
        help="ignore Git tags before this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--window-days",
        type=_positive_int,
        default=7,
        help="release window length ending on tag day (default: 7)",
    )
    parser.add_argument(
        "--baseline-weeks",
        type=_positive_int,
        default=8,
        help="number of prior weekday-matched weeks (default: 8)",
    )
    parser.add_argument(
        "--min-window-samples",
        type=_positive_int,
        default=4,
        help="minimum observed days for a release-window metric (default: 4)",
    )
    parser.add_argument(
        "--min-baseline-samples",
        type=_positive_int,
        default=12,
        help="minimum observed days for a baseline metric (default: 12)",
    )
    parser.add_argument(
        "--max-depth",
        type=_positive_int,
        default=4,
        help="maximum repository discovery depth (default: 4)",
    )
    parser.add_argument(
        "--max-repositories",
        type=_positive_int,
        default=200,
        help="hard repository discovery limit (default: 200)",
    )
    parser.add_argument(
        "--max-releases",
        type=_positive_int,
        default=5000,
        help="hard Git tag limit (default: 5000)",
    )
    parser.add_argument(
        "--max-health-gib",
        type=_positive_int,
        default=4,
        help="maximum uncompressed Apple XML size in GiB (default: 4)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _write_private_atomic(path: Path, content: str) -> None:
    parent = path.expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        with suppress(OSError):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path.expanduser())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _status(message: str) -> None:
    if sys.stderr.isatty():
        print(message, file=sys.stderr, flush=True)


def _present_show(content: str) -> None:
    """Write a deliberately paced result for terminal screen recordings."""

    for line in content.splitlines():
        print(line, flush=True)
        time.sleep(0.14 if not line else 0.045)


def run(args: argparse.Namespace) -> int:
    if args.show and args.output:
        raise ValueError("--show cannot be combined with --output")
    if args.show and args.format != "text":
        raise ValueError("--show supports text output only")
    if args.show and args.plain:
        raise ValueError("--show cannot be combined with --plain")

    _status("[1/3] Opening Apple Health. Nothing leaves this machine…")
    health = load_apple_health(
        args.health_export.expanduser(),
        max_uncompressed_bytes=args.max_health_gib * 1024**3,
    )
    repositories = discover_repositories(
        (path.expanduser() for path in args.repositories),
        max_depth=args.max_depth,
        max_repositories=args.max_repositories,
    )
    if not repositories:
        raise GitReadError("no Git repositories found in the supplied paths")
    _status(f"[2/3] Reading {len(repositories)} repo(s). Git remembers when you shipped…")
    releases = load_releases(
        repositories,
        since=args.since,
        max_releases=args.max_releases,
    )
    config = AnalysisConfig(
        window_days=args.window_days,
        baseline_weeks=args.baseline_weeks,
        min_window_samples=args.min_window_samples,
        min_baseline_samples=args.min_baseline_samples,
    )
    _status("[3/3] Matching timelines. Coincidence gets a spreadsheet…")
    report = analyze_releases(health, releases, config=config)
    terminal_reveal = (
        args.format == "text"
        and not args.output
        and not args.plain
        and (sys.stdout.isatty() or args.show)
    )
    if terminal_reveal:
        use_color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
        output = render_terminal(report, color=use_color)
    else:
        output = render(report, args.format)
    if args.output:
        _write_private_atomic(args.output, output)
    elif args.show:
        _present_show(output)
    else:
        sys.stdout.write(output)
    return 0 if report.impacts else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (AppleHealthError, GitReadError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
