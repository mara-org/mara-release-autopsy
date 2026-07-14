from __future__ import annotations

import json
import os
import stat
import subprocess
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from mara_autopsy.cli import main


def _apple_datetime(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="seconds")


def _write_health_xml(path: Path, start: date, end: date, release_day: date) -> None:
    records: list[str] = []
    day = start
    adverse_start = release_day - timedelta(days=6)
    while day <= end:
        adverse = adverse_start <= day <= release_day
        end_at = datetime.combine(day, time(7), tzinfo=timezone.utc)
        start_at = end_at - timedelta(hours=6 if adverse else 8)
        step_value = 5_000 if adverse else 10_000
        resting_hr = 66 if adverse else 60
        records.extend(
            [
                (
                    '<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
                    'sourceName="Synthetic Sleep" '
                    f'startDate="{_apple_datetime(start_at)}" '
                    f'endDate="{_apple_datetime(end_at)}" '
                    'value="HKCategoryValueSleepAnalysisAsleep"/>'
                ),
                (
                    '<Record type="HKQuantityTypeIdentifierStepCount" '
                    'sourceName="Synthetic Steps" '
                    f'startDate="{_apple_datetime(end_at)}" '
                    f'endDate="{_apple_datetime(end_at)}" value="{step_value}"/>'
                ),
                (
                    '<Record type="HKQuantityTypeIdentifierRestingHeartRate" '
                    'sourceName="Synthetic Heart" '
                    f'startDate="{_apple_datetime(end_at)}" '
                    f'endDate="{_apple_datetime(end_at)}" value="{resting_hr}"/>'
                ),
            ]
        )
        day += timedelta(days=1)
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData>\n'
        + "\n".join(records)
        + "\n</HealthData>\n",
        encoding="utf-8",
    )


def _write_health_zip(path: Path, start: date, end: date, release_day: date) -> None:
    xml_path = path.with_suffix(".xml")
    _write_health_xml(xml_path, start, end, release_day)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(xml_path, "apple_health_export/export.xml")
    xml_path.unlink()


def _git(repository: Path, *arguments: str, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def _create_tagged_repository(path: Path, release_day: date) -> None:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Synthetic Test")
    _git(path, "config", "user.email", "synthetic@example.invalid")
    (path / "README.md").write_text("synthetic fixture\n", encoding="utf-8")
    _git(path, "add", "README.md")
    timestamp = datetime.combine(release_day, time(12), tzinfo=timezone.utc).isoformat()
    environment = {**os.environ, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp}
    _git(path, "commit", "--quiet", "-m", "Synthetic release", env=environment)
    _git(path, "tag", "-a", "v1.0.0", "-m", "Synthetic release", env=environment)


def test_cli_runs_the_real_health_git_analysis_pipeline(tmp_path: Path) -> None:
    release_day = date(2025, 3, 31)
    health_zip = tmp_path / "export.zip"
    repository = tmp_path / "payments-api"
    output = tmp_path / "report.json"
    _write_health_zip(health_zip, date(2025, 1, 1), release_day, release_day)
    assert not health_zip.with_suffix(".xml").exists()
    _create_tagged_repository(repository, release_day)

    exit_code = main(
        [os.fspath(health_zip), os.fspath(repository), "--format", "json", "--output", str(output)]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["impacts"][0]["repository"] == "payments-api"
    assert payload["impacts"][0]["tag"] == "v1.0.0"
    comparisons = {item["metric"]: item for item in payload["impacts"][0]["comparisons"]}
    assert comparisons["sleep_minutes"]["difference"] == pytest.approx(-120.0)
    assert comparisons["bedtime_minute"]["difference"] == pytest.approx(120.0)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_cli_returns_one_when_no_release_has_enough_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    health_xml = tmp_path / "export.xml"
    repository = tmp_path / "untagged"
    _write_health_xml(health_xml, date(2025, 1, 1), date(2025, 1, 10), date(2025, 1, 10))
    repository.mkdir()
    _git(repository, "init", "--quiet")

    exit_code = main([str(health_xml), str(repository)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "0 analyzable / 0 found" in captured.out
    assert "does not establish causation" in captured.out
    assert captured.err == ""


def test_cli_reports_invalid_health_input_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "export.xml"
    repository = tmp_path / "repository"
    invalid.write_text("<secret>private value</secret>", encoding="utf-8")
    repository.mkdir()
    _git(repository, "init", "--quiet")

    exit_code = main([str(invalid), str(repository)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "private value" not in captured.err


def test_version_exits_without_requiring_inputs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out.startswith("mara-autopsy 0.1.0")


def test_show_mode_uses_terminal_reveal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_day = date(2025, 3, 31)
    health_zip = tmp_path / "export.zip"
    repository = tmp_path / "payments-api"
    _write_health_zip(health_zip, date(2025, 1, 1), release_day, release_day)
    _create_tagged_repository(repository, release_day)
    monkeypatch.setattr("mara_autopsy.cli.time.sleep", lambda _seconds: None)

    exit_code = main([str(health_zip), str(repository), "--show", "--no-color"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "MARA // RELEASE AUTOPSY" in captured.out
    assert "your body kept the receipts" in captured.out
    assert "\x1b" not in captured.out


def test_show_mode_rejects_file_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            str(tmp_path / "missing.zip"),
            str(tmp_path),
            "--show",
            "--output",
            str(tmp_path / "report.txt"),
        ]
    )

    assert exit_code == 2
    assert "--show cannot be combined with --output" in capsys.readouterr().err
