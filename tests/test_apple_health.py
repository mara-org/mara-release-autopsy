from __future__ import annotations

import warnings
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from mara_autopsy.apple_health import AppleHealthError, load_apple_health


def _record(**attributes: str) -> str:
    rendered = " ".join(f"{key}={quoteattr(value)}" for key, value in attributes.items())
    return f"<Record {rendered} />"


def _health_xml(*records: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<HealthData locale="en_US">{"".join(records)}</HealthData>'
    ).encode()


def _write_xml(path: Path, *records: str) -> Path:
    path.write_bytes(_health_xml(*records))
    return path


def _base_record(record_type: str, source: str, start: str, end: str, value: str) -> str:
    return _record(
        type=record_type,
        sourceName=source,
        startDate=start,
        endDate=end,
        value=value,
    )


def test_streams_and_normalizes_supported_metrics_without_double_counting(tmp_path: Path) -> None:
    sleep_type = "HKCategoryTypeIdentifierSleepAnalysis"
    steps_type = "HKQuantityTypeIdentifierStepCount"
    rhr_type = "HKQuantityTypeIdentifierRestingHeartRate"
    records = [
        _base_record(
            sleep_type,
            "Watch",
            "2024-01-01 22:00:00 +0000",
            "2024-01-02 02:00:00 +0000",
            "HKCategoryValueSleepAnalysisAsleepCore",
        ),
        _base_record(
            sleep_type,
            "Ring",
            "2024-01-02 01:00:00 +0000",
            "2024-01-02 07:00:00 +0000",
            "HKCategoryValueSleepAnalysisAsleepUnspecified",
        ),
        _base_record(
            sleep_type,
            "Watch",
            "2024-01-01 21:45:00 +0000",
            "2024-01-02 07:15:00 +0000",
            "HKCategoryValueSleepAnalysisInBed",
        ),
        _base_record(
            steps_type,
            "Phone",
            "2024-01-02 08:00:00 +0000",
            "2024-01-02 09:00:00 +0000",
            "100",
        ),
        _base_record(
            steps_type,
            "Phone",
            "2024-01-02 08:00:00 +0000",
            "2024-01-02 09:00:00 +0000",
            "100",
        ),
        _base_record(
            steps_type,
            "Phone",
            "2024-01-02 10:00:00 +0000",
            "2024-01-02 11:00:00 +0000",
            "200",
        ),
        _base_record(
            steps_type,
            "Watch",
            "2024-01-02 08:00:00 +0000",
            "2024-01-02 09:00:00 +0000",
            "100",
        ),
        _base_record(
            steps_type,
            "Watch",
            "2024-01-02 10:00:00 +0000",
            "2024-01-02 11:00:00 +0000",
            "200",
        ),
        _base_record(
            rhr_type,
            "Watch",
            "2024-01-02 07:00:00 +0000",
            "2024-01-02 07:00:00 +0000",
            "60",
        ),
        _base_record(
            rhr_type,
            "Watch",
            "2024-01-02 07:05:00 +0000",
            "2024-01-02 07:05:00 +0000",
            "62",
        ),
        _base_record(
            rhr_type,
            "Ring",
            "2024-01-02 07:00:00 +0000",
            "2024-01-02 07:00:00 +0000",
            "70",
        ),
        _base_record(
            "HKQuantityTypeIdentifierHeartRate",
            "Watch",
            "2024-01-02 07:00:00 +0000",
            "2024-01-02 07:00:00 +0000",
            "75",
        ),
    ]

    dataset = load_apple_health(_write_xml(tmp_path / "export.xml", *records))

    assert dataset.records_seen == 12
    assert dataset.records_used == 10
    assert len(dataset.days) == 1
    day = dataset.days[0]
    assert day.day.isoformat() == "2024-01-02"
    assert day.sleep_minutes == pytest.approx(540.0)
    assert day.bedtime_minute == pytest.approx(21 * 60 + 45)
    assert day.steps == 300.0
    assert day.resting_heart_rate == pytest.approx(61.0)
    assert dataset.sources_by_metric == {
        "sleep_minutes": ("Ring", "Watch"),
        "bedtime_minute": ("Ring", "Watch"),
        "steps": ("Phone", "Watch"),
        "resting_heart_rate": ("Ring", "Watch"),
    }
    assert any("duplicate" in warning for warning in dataset.warnings)
    assert any("overlapping asleep" in warning for warning in dataset.warnings)
    assert any("competing step" in warning for warning in dataset.warnings)
    assert any("competing resting-heart-rate" in warning for warning in dataset.warnings)


def test_pre_midnight_sleep_stage_is_bucketed_with_wake_day(tmp_path: Path) -> None:
    sleep_type = "HKCategoryTypeIdentifierSleepAnalysis"
    dataset = load_apple_health(
        _write_xml(
            tmp_path / "export.xml",
            _base_record(
                sleep_type,
                "Watch",
                "2024-06-01 23:00:00 +0300",
                "2024-06-01 23:45:00 +0300",
                "HKCategoryValueSleepAnalysisAsleepCore",
            ),
            _base_record(
                sleep_type,
                "Watch",
                "2024-06-02 00:00:00 +0300",
                "2024-06-02 06:00:00 +0300",
                "HKCategoryValueSleepAnalysisAsleepDeep",
            ),
        )
    )

    assert len(dataset.days) == 1
    assert dataset.days[0].day.isoformat() == "2024-06-02"
    assert dataset.days[0].sleep_minutes == 405.0
    assert dataset.days[0].bedtime_minute == 23 * 60


def test_bedtime_comes_from_main_sleep_session_not_an_afternoon_nap(tmp_path: Path) -> None:
    sleep_type = "HKCategoryTypeIdentifierSleepAnalysis"
    dataset = load_apple_health(
        _write_xml(
            tmp_path / "export.xml",
            _base_record(
                sleep_type,
                "Watch",
                "2024-06-01 13:00:00 +0300",
                "2024-06-01 14:00:00 +0300",
                "HKCategoryValueSleepAnalysisAsleepCore",
            ),
            _base_record(
                sleep_type,
                "Watch",
                "2024-06-01 23:00:00 +0300",
                "2024-06-02 02:00:00 +0300",
                "HKCategoryValueSleepAnalysisAsleepCore",
            ),
            _base_record(
                sleep_type,
                "Watch",
                "2024-06-02 02:30:00 +0300",
                "2024-06-02 07:00:00 +0300",
                "HKCategoryValueSleepAnalysisAsleepREM",
            ),
        )
    )

    assert len(dataset.days) == 1
    assert dataset.days[0].sleep_minutes == 510.0
    assert dataset.days[0].bedtime_minute == 23 * 60


def test_malformed_supported_records_warn_but_unsupported_records_are_ignored(
    tmp_path: Path,
) -> None:
    dataset = load_apple_health(
        _write_xml(
            tmp_path / "export.xml",
            _base_record(
                "HKQuantityTypeIdentifierStepCount",
                "Phone",
                "2024-01-01 08:00:00 +0000",
                "2024-01-01 09:00:00 +0000",
                "not-a-number",
            ),
            _record(
                type="HKQuantityTypeIdentifierRestingHeartRate",
                sourceName="Watch",
                startDate="not-a-date",
                endDate="2024-01-01 09:00:00 +0000",
                value="60",
            ),
            _base_record(
                "HKCategoryTypeIdentifierSleepAnalysis",
                "Watch",
                "2024-01-01 22:00:00 +0000",
                "2024-01-02 07:00:00 +0000",
                "FutureSleepCategory",
            ),
            _base_record(
                "HKQuantityTypeIdentifierBodyMass",
                "Scale",
                "2024-01-01 09:00:00 +0000",
                "2024-01-01 09:00:00 +0000",
                "80",
            ),
        )
    )

    assert dataset.records_seen == 4
    assert dataset.records_used == 0
    assert dataset.days == ()
    assert any("invalid numeric" in warning for warning in dataset.warnings)
    assert any("invalid timestamps" in warning for warning in dataset.warnings)
    assert any("unknown category" in warning for warning in dataset.warnings)


def test_reads_nested_export_xml_from_zip_without_extraction(tmp_path: Path) -> None:
    archive_path = tmp_path / "apple-health.zip"
    payload = _health_xml(
        _base_record(
            "HKQuantityTypeIdentifierStepCount",
            "Phone",
            "2024-01-01 08:00:00 +0000",
            "2024-01-01 09:00:00 +0000",
            "1234",
        )
    )
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("apple_health_export/export.xml", payload)
        archive.writestr("apple_health_export/workout-routes/route.gpx", b"route")

    dataset = load_apple_health(archive_path)

    assert dataset.days[0].steps == 1234.0
    assert not (tmp_path / "apple_health_export").exists()


def test_rejects_zip_member_whose_declared_size_exceeds_limit(tmp_path: Path) -> None:
    archive_path = tmp_path / "oversized.zip"
    payload = _health_xml() + b" " * 1_000
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("export.xml", payload)

    with pytest.raises(AppleHealthError, match=r"declares.*above"):
        load_apple_health(archive_path, max_uncompressed_bytes=len(payload) - 1)


@pytest.mark.parametrize("unsafe_name", ["../escape.txt", "/absolute.txt", "dir\\evil.txt"])
def test_rejects_unsafe_zip_paths(tmp_path: Path, unsafe_name: str) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("export.xml", _health_xml())
        archive.writestr(unsafe_name, b"unsafe")

    with pytest.raises(AppleHealthError, match=r"unsafe|backslash"):
        load_apple_health(archive_path)


def test_rejects_ambiguous_and_duplicate_zip_entries(tmp_path: Path) -> None:
    ambiguous = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(ambiguous, "w") as archive:
        archive.writestr("one/export.xml", _health_xml())
        archive.writestr("two/EXPORT.XML", _health_xml())
    with pytest.raises(AppleHealthError, match="ambiguous"):
        load_apple_health(ambiguous)

    duplicate = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("export.xml", _health_xml())
            archive.writestr("export.xml", _health_xml())
    with pytest.raises(AppleHealthError, match="duplicate member"):
        load_apple_health(duplicate)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"<HealthData><Record>", "malformed"),
        (b"<NotHealthData />", "root element"),
        (
            b'<!DOCTYPE HealthData [<!ENTITY x "boom">]><HealthData>&x;</HealthData>',
            "DTD or entity",
        ),
    ],
)
def test_rejects_invalid_top_level_xml(tmp_path: Path, payload: bytes, message: str) -> None:
    path = tmp_path / "export.xml"
    path.write_bytes(payload)

    with pytest.raises(AppleHealthError, match=message):
        load_apple_health(path)


def test_rejects_raw_xml_over_size_limit_and_invalid_limit(tmp_path: Path) -> None:
    path = tmp_path / "export.xml"
    path.write_bytes(_health_xml())

    with pytest.raises(AppleHealthError, match="exceeds"):
        load_apple_health(path, max_uncompressed_bytes=10)
    with pytest.raises(AppleHealthError, match="positive integer"):
        load_apple_health(path, max_uncompressed_bytes=0)
