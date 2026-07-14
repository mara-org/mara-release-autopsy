"""Streaming, local-only parser for Apple Health exports.

The native export contains raw samples from every source.  In particular, it
does not contain Health's source-priority decisions.  This module therefore
uses conservative rules: asleep intervals are unioned across sources, while
additive daily values are selected from one source instead of being summed
across competing sources.
"""

from __future__ import annotations

import math
import stat
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, ClassVar
from xml.etree import ElementTree

from mara_autopsy.models import DailyHealth, HealthDataset

_SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
_STEPS_TYPE = "HKQuantityTypeIdentifierStepCount"
_RESTING_HEART_RATE_TYPE = "HKQuantityTypeIdentifierRestingHeartRate"
_SUPPORTED_TYPES = {_SLEEP_TYPE, _STEPS_TYPE, _RESTING_HEART_RATE_TYPE}

_IN_BED_VALUES = {"0", "HKCategoryValueSleepAnalysisInBed"}
_AWAKE_VALUES = {"2", "HKCategoryValueSleepAnalysisAwake"}
_ASLEEP_NUMERIC_VALUES = {"1", "3", "4", "5"}

_MAX_ZIP_MEMBERS = 100_000
_MAX_ZIP_PATH_LENGTH = 4_096
_MAX_XML_DEPTH = 128
_MAX_ATTRIBUTE_LENGTH = 16_384
_MAX_SUPPORTED_RECORDS = 2_000_000
_MAX_SLEEP_INTERVAL_SECONDS = 48 * 60 * 60
_MAX_SLEEP_SESSION_GAP_SECONDS = 90 * 60
_DTD_MARKERS = (b"<!doctype", b"<!entity")
_SCAN_TAIL_BYTES = max(len(marker) for marker in _DTD_MARKERS) - 1


class AppleHealthError(ValueError):
    """Raised when an Apple Health input cannot be parsed safely."""


class _GuardedReader:
    """Count decompressed bytes and reject DTD/entity declarations."""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self._bytes_read = 0
        self._scan_tail = b""

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        if not isinstance(data, bytes):
            raise AppleHealthError("Apple Health input did not produce binary data")

        self._bytes_read += len(data)
        if self._bytes_read > self._limit:
            raise AppleHealthError(f"Apple Health XML exceeds the {self._limit} byte safety limit")

        probe = (self._scan_tail + data).lower()
        if any(marker in probe for marker in _DTD_MARKERS):
            raise AppleHealthError("Apple Health XML must not contain DTD or entity declarations")
        self._scan_tail = probe[-_SCAN_TAIL_BYTES:]
        return data


class _WarningCounts:
    _MESSAGES: ClassVar[dict[str, str]] = {
        "bad_placement": "Ignored {count} Record element(s) outside an Apple Health container.",
        "duplicate": "Ignored {count} duplicate supported record(s).",
        "invalid_interval": "Ignored {count} supported record(s) with invalid time intervals.",
        "invalid_number": "Ignored {count} supported record(s) with invalid numeric values.",
        "invalid_timestamp": "Ignored {count} supported record(s) with invalid timestamps.",
        "missing_source": "Accepted {count} supported record(s) without a source name.",
        "oversized_attribute": "Ignored {count} supported record(s) with oversized attributes.",
        "unknown_sleep": "Ignored {count} sleep record(s) with unknown category values.",
    }

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def add(self, key: str) -> None:
        self._counts[key] += 1

    def render(self) -> list[str]:
        return [self._MESSAGES[key].format(count=self._counts[key]) for key in sorted(self._counts)]


class _Accumulator:
    def __init__(self) -> None:
        self.records_seen = 0
        self.records_used = 0
        self.warning_counts = _WarningCounts()
        self._deduplication_keys: set[tuple[object, ...]] = set()

        self.sleep_intervals: dict[date, list[tuple[float, float, str]]] = {}
        self.in_bed_candidates: dict[date, list[tuple[float, float, float, str]]] = {}
        self.asleep_bedtime_candidates: dict[date, list[tuple[float, float, str]]] = {}
        self.steps: dict[date, dict[str, list[float]]] = {}
        self.resting_heart_rate: dict[date, dict[str, list[float]]] = {}
        self.sources: dict[str, set[str]] = {
            "sleep_minutes": set(),
            "bedtime_minute": set(),
            "steps": set(),
            "resting_heart_rate": set(),
        }

    def consume(self, attributes: dict[str, str]) -> None:
        record_type = attributes.get("type")
        if record_type not in _SUPPORTED_TYPES:
            return
        if any(len(key) + len(value) > _MAX_ATTRIBUTE_LENGTH for key, value in attributes.items()):
            self.warning_counts.add("oversized_attribute")
            return

        source = attributes.get("sourceName", "").strip()
        if not source:
            source = "(unknown source)"
            self.warning_counts.add("missing_source")

        start = _parse_datetime(attributes.get("startDate"))
        end = _parse_datetime(attributes.get("endDate"))
        if start is None or end is None:
            self.warning_counts.add("invalid_timestamp")
            return

        start_tick = _timeline_seconds(start)
        end_tick = _timeline_seconds(end)
        if end_tick < start_tick:
            self.warning_counts.add("invalid_interval")
            return

        if record_type == _SLEEP_TYPE:
            self._consume_sleep(attributes, source, start, end, start_tick, end_tick)
        elif record_type == _STEPS_TYPE:
            self._consume_steps(attributes, source, end, start_tick, end_tick)
        else:
            self._consume_resting_heart_rate(attributes, source, end, start_tick, end_tick)

    def _consume_sleep(
        self,
        attributes: dict[str, str],
        source: str,
        start: datetime,
        end: datetime,
        start_tick: float,
        end_tick: float,
    ) -> None:
        if end_tick <= start_tick or end_tick - start_tick > _MAX_SLEEP_INTERVAL_SECONDS:
            self.warning_counts.add("invalid_interval")
            return

        value = attributes.get("value", "").strip()
        if value in _IN_BED_VALUES:
            category = "in_bed"
        elif value in _AWAKE_VALUES:
            return
        elif value in _ASLEEP_NUMERIC_VALUES or value.startswith(
            "HKCategoryValueSleepAnalysisAsleep"
        ):
            category = "asleep"
        else:
            self.warning_counts.add("unknown_sleep")
            return

        key = ("sleep", category, source, start_tick, end_tick)
        if not self._accept_unique(key):
            return

        # A sleep day runs from noon on the preceding calendar day to noon on
        # this day.  This keeps pre-midnight and post-midnight stage records in
        # the same nightly bucket without requiring vendor-specific sessions.
        sleep_day = (end + timedelta(hours=12)).date()
        bedtime_minute = _minute_of_day(start)
        candidate = (start_tick, bedtime_minute, source)

        if category == "in_bed":
            self.in_bed_candidates.setdefault(sleep_day, []).append(
                (start_tick, end_tick, bedtime_minute, source)
            )
            self.sources["bedtime_minute"].add(source)
            return

        self.sleep_intervals.setdefault(sleep_day, []).append((start_tick, end_tick, source))
        self.asleep_bedtime_candidates.setdefault(sleep_day, []).append(candidate)
        self.sources["sleep_minutes"].add(source)
        self.sources["bedtime_minute"].add(source)

    def _consume_steps(
        self,
        attributes: dict[str, str],
        source: str,
        end: datetime,
        start_tick: float,
        end_tick: float,
    ) -> None:
        value = _parse_finite_float(attributes.get("value"))
        if value is None or value < 0:
            self.warning_counts.add("invalid_number")
            return
        key = ("steps", source, start_tick, end_tick, value)
        if not self._accept_unique(key):
            return

        per_source = self.steps.setdefault(end.date(), {})
        aggregate = per_source.setdefault(source, [0.0, 0.0])
        aggregate[0] += value
        aggregate[1] += 1.0
        self.sources["steps"].add(source)

    def _consume_resting_heart_rate(
        self,
        attributes: dict[str, str],
        source: str,
        end: datetime,
        start_tick: float,
        end_tick: float,
    ) -> None:
        value = _parse_finite_float(attributes.get("value"))
        if value is None or value <= 0:
            self.warning_counts.add("invalid_number")
            return
        key = ("resting_heart_rate", source, start_tick, end_tick, value)
        if not self._accept_unique(key):
            return

        per_source = self.resting_heart_rate.setdefault(end.date(), {})
        aggregate = per_source.setdefault(source, [0.0, 0.0])
        aggregate[0] += value
        aggregate[1] += 1.0
        self.sources["resting_heart_rate"].add(source)

    def _accept_unique(self, key: tuple[object, ...]) -> bool:
        if key in self._deduplication_keys:
            self.warning_counts.add("duplicate")
            return False
        if len(self._deduplication_keys) >= _MAX_SUPPORTED_RECORDS:
            raise AppleHealthError(
                f"Apple Health input exceeds {_MAX_SUPPORTED_RECORDS} supported records"
            )
        self._deduplication_keys.add(key)
        self.records_used += 1
        return True

    def finish(self) -> HealthDataset:
        all_days = sorted(
            set(self.sleep_intervals)
            | set(self.in_bed_candidates)
            | set(self.asleep_bedtime_candidates)
            | set(self.steps)
            | set(self.resting_heart_rate)
        )
        days: list[DailyHealth] = []
        overlapping_sleep_days = 0
        multiple_step_source_days = 0
        multiple_rhr_source_days = 0

        for day in all_days:
            sleep_minutes: float | None = None
            intervals = self.sleep_intervals.get(day)
            if intervals:
                raw_seconds = math.fsum(end - start for start, end, _source in intervals)
                merged_seconds = _merged_interval_seconds(intervals)
                if merged_seconds + 1e-6 < raw_seconds:
                    overlapping_sleep_days += 1
                sleep_minutes = merged_seconds / 60.0

            in_bed = self.in_bed_candidates.get(day)
            if in_bed:
                session_intervals = [(start, end, source) for start, end, _bed, source in in_bed]
                bedtime_candidates = [(start, bed, source) for start, _end, bed, source in in_bed]
            else:
                session_intervals = intervals or []
                bedtime_candidates = self.asleep_bedtime_candidates.get(day, [])
            bedtime_minute = _main_session_bedtime(session_intervals, bedtime_candidates)

            step_value: float | None = None
            step_sources = self.steps.get(day)
            if step_sources:
                if len(step_sources) > 1:
                    multiple_step_source_days += 1
                _source, selected = min(
                    step_sources.items(),
                    key=lambda item: (-item[1][0], -item[1][1], item[0]),
                )
                step_value = selected[0]

            resting_heart_rate: float | None = None
            rhr_sources = self.resting_heart_rate.get(day)
            if rhr_sources:
                if len(rhr_sources) > 1:
                    multiple_rhr_source_days += 1
                _source, selected = min(
                    rhr_sources.items(),
                    key=lambda item: (-item[1][1], item[0]),
                )
                resting_heart_rate = selected[0] / selected[1]

            days.append(
                DailyHealth(
                    day=day,
                    sleep_minutes=sleep_minutes,
                    bedtime_minute=bedtime_minute,
                    steps=step_value,
                    resting_heart_rate=resting_heart_rate,
                )
            )

        warnings = self.warning_counts.render()
        if overlapping_sleep_days:
            warnings.append(
                "Merged overlapping asleep intervals on "
                f"{overlapping_sleep_days} day(s) to prevent double counting."
            )
        if multiple_step_source_days:
            warnings.append(
                "Resolved competing step sources on "
                f"{multiple_step_source_days} day(s) by selecting one daily source."
            )
        if multiple_rhr_source_days:
            warnings.append(
                "Resolved competing resting-heart-rate sources on "
                f"{multiple_rhr_source_days} day(s) by selecting one daily source."
            )

        sources_by_metric = {
            metric: tuple(sorted(metric_sources))
            for metric, metric_sources in self.sources.items()
            if metric_sources
        }
        return HealthDataset(
            days=tuple(days),
            records_seen=self.records_seen,
            records_used=self.records_used,
            sources_by_metric=sources_by_metric,
            warnings=tuple(warnings),
        )


def load_apple_health(
    path: str | Path,
    *,
    max_uncompressed_bytes: int = 4_000_000_000,
) -> HealthDataset:
    """Load an Apple Health ``export.xml`` or its containing ZIP archive.

    ZIP members are validated and streamed directly from the archive.  Nothing
    is extracted to disk, and the parser never builds the full XML tree.
    """

    if (
        isinstance(max_uncompressed_bytes, bool)
        or not isinstance(max_uncompressed_bytes, int)
        or max_uncompressed_bytes <= 0
    ):
        raise AppleHealthError("max_uncompressed_bytes must be a positive integer")

    try:
        input_path = Path(path)
    except TypeError as exc:
        raise AppleHealthError("Apple Health path must be a string or Path") from exc

    try:
        file_stat = input_path.stat()
    except OSError as exc:
        raise AppleHealthError(f"Cannot access Apple Health input: {exc}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise AppleHealthError("Apple Health input must be a regular file")

    with _open_apple_health_payload(input_path, max_uncompressed_bytes) as payload:
        return _parse_xml(payload)


@contextmanager
def _open_apple_health_payload(path: Path, limit: int) -> Iterator[_GuardedReader]:
    try:
        is_zip = zipfile.is_zipfile(path)
    except OSError as exc:
        raise AppleHealthError(f"Cannot inspect Apple Health input: {exc}") from exc

    if not is_zip:
        if path.suffix.lower() == ".zip":
            raise AppleHealthError("Apple Health ZIP is invalid or truncated")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AppleHealthError(f"Cannot inspect Apple Health XML: {exc}") from exc
        if size > limit:
            raise AppleHealthError(f"Apple Health XML exceeds the {limit} byte safety limit")
        try:
            with path.open("rb") as raw:
                yield _GuardedReader(raw, limit)
        except OSError as exc:
            raise AppleHealthError(f"Cannot read Apple Health XML: {exc}") from exc
        return

    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ZIP_MEMBERS:
                raise AppleHealthError(f"Apple Health ZIP has more than {_MAX_ZIP_MEMBERS} members")

            seen_names: set[str] = set()
            candidates: list[zipfile.ZipInfo] = []
            for member in members:
                _validate_zip_member(member)
                if member.filename in seen_names:
                    raise AppleHealthError("Apple Health ZIP contains duplicate member names")
                seen_names.add(member.filename)
                if (
                    not member.is_dir()
                    and PurePosixPath(member.filename).name.lower() == "export.xml"
                ):
                    candidates.append(member)

            if not candidates:
                raise AppleHealthError("Apple Health ZIP does not contain export.xml")
            if len(candidates) != 1:
                raise AppleHealthError("Apple Health ZIP contains ambiguous export.xml entries")

            export_member = candidates[0]
            if export_member.file_size > limit:
                raise AppleHealthError(
                    "Apple Health export.xml declares "
                    f"{export_member.file_size} bytes, above the {limit} byte safety limit"
                )
            try:
                with archive.open(export_member, "r") as raw:
                    yield _GuardedReader(raw, limit)
            except (NotImplementedError, RuntimeError) as exc:
                raise AppleHealthError(f"Cannot decode Apple Health export.xml: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise AppleHealthError(f"Apple Health ZIP is invalid: {exc}") from exc
    except OSError as exc:
        raise AppleHealthError(f"Cannot read Apple Health ZIP: {exc}") from exc


def _validate_zip_member(member: zipfile.ZipInfo) -> None:
    name = member.filename
    if not name or "\x00" in name or len(name) > _MAX_ZIP_PATH_LENGTH:
        raise AppleHealthError("Apple Health ZIP contains an unsafe member name")
    if "\\" in name:
        raise AppleHealthError("Apple Health ZIP contains a backslash path")

    trimmed = name[:-1] if member.is_dir() and name.endswith("/") else name
    parts = trimmed.split("/")
    path = PurePosixPath(trimmed)
    if (
        not trimmed
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or (parts and ":" in parts[0])
    ):
        raise AppleHealthError(f"Apple Health ZIP contains an unsafe path: {name!r}")
    if member.flag_bits & 0x1:
        raise AppleHealthError("Apple Health ZIP contains an encrypted member")

    unix_mode = member.external_attr >> 16
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise AppleHealthError("Apple Health ZIP contains a symbolic link")


def _parse_xml(payload: _GuardedReader) -> HealthDataset:
    accumulator = _Accumulator()
    stack: list[str] = []
    root_seen = False
    root_closed = False

    try:
        for event, element in ElementTree.iterparse(payload, events=("start", "end")):
            if event == "start":
                if not root_seen:
                    if element.tag != "HealthData":
                        raise AppleHealthError("Apple Health XML root element must be HealthData")
                    root_seen = True
                stack.append(element.tag)
                if len(stack) > _MAX_XML_DEPTH:
                    raise AppleHealthError(
                        f"Apple Health XML nesting exceeds {_MAX_XML_DEPTH} elements"
                    )
                continue

            if element.tag == "Record":
                accumulator.records_seen += 1
                parent = stack[-2] if len(stack) >= 2 else None
                if parent not in {"HealthData", "Correlation"}:
                    accumulator.warning_counts.add("bad_placement")
                else:
                    accumulator.consume(element.attrib)

            if len(stack) == 1 and element.tag == "HealthData":
                root_closed = True
            element.clear()
            if stack:
                stack.pop()
    except ElementTree.ParseError as exc:
        raise AppleHealthError(f"Apple Health XML is malformed: {exc}") from exc

    if not root_seen or not root_closed:
        raise AppleHealthError("Apple Health XML is empty or incomplete")
    return accumulator.finish()


def _parse_datetime(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith(("Z", "z")):
        value = f"{value[:-1]}+00:00"
    # Apple exports offsets as ``+0300``.  Python 3.11+ accepts that form,
    # while Python 3.10 (which this project supports) requires ``+03:00``.
    elif len(value) >= 5 and value[-5] in "+-" and value[-4:].isdigit():
        value = f"{value[:-5]}{value[-5:-2]}:{value[-2:]}"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_finite_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _timeline_seconds(value: datetime) -> float:
    if value.tzinfo is None:
        epoch = datetime(1970, 1, 1)
        return (value - epoch).total_seconds()
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (value.astimezone(timezone.utc) - epoch).total_seconds()


def _minute_of_day(value: datetime) -> float:
    return value.hour * 60 + value.minute + value.second / 60.0 + value.microsecond / 60_000_000.0


def _merged_interval_seconds(intervals: list[tuple[float, float, str]]) -> float:
    ordered = sorted((start, end) for start, end, _source in intervals)
    current_start, current_end = ordered[0]
    merged: list[tuple[float, float]] = []
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return math.fsum(end - start for start, end in merged)


def _main_session_bedtime(
    intervals: list[tuple[float, float, str]],
    candidates: list[tuple[float, float, str]],
) -> float | None:
    """Return the bedtime of the longest sleep session, excluding shorter naps."""

    if not intervals or not candidates:
        return None
    ordered = sorted((start, end) for start, end, _source in intervals)
    session_start, session_end = ordered[0]
    covered_seconds = session_end - session_start
    sessions: list[tuple[float, float, float]] = []

    for start, end in ordered[1:]:
        if start <= session_end + _MAX_SLEEP_SESSION_GAP_SECONDS:
            if start < session_end:
                covered_seconds += max(0.0, end - session_end)
            else:
                covered_seconds += end - start
            session_end = max(session_end, end)
        else:
            sessions.append((session_start, session_end, covered_seconds))
            session_start, session_end = start, end
            covered_seconds = end - start
    sessions.append((session_start, session_end, covered_seconds))

    main_start, main_end, _covered = max(
        sessions,
        key=lambda session: (session[2], session[1] - session[0], session[1]),
    )
    matching = [candidate for candidate in candidates if main_start <= candidate[0] <= main_end]
    if not matching:
        return None
    return min(matching, key=lambda item: (item[0], item[2]))[1]
