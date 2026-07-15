"""Bounded, read-only access to release tags in local Git repositories."""

from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Iterable
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from typing import IO

from mara_autopsy.models import ReleaseEvent

_GIT_EXECUTABLE = "git"
_MAX_GIT_STDOUT_BYTES = 32 * 1024 * 1024
_MAX_GIT_STDERR_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_MAX_DISPLAY_NAME_CHARS = 120

_SKIPPED_DIRECTORY_NAMES = frozenset(
    name.casefold()
    for name in (
        "__pycache__",
        "build",
        "coverage",
        "deriveddata",
        "dist",
        "env",
        "node_modules",
        "pods",
        "target",
        "vendor",
        "vendors",
        "venv",
    )
)

_ISO_STRICT_DATE = re.compile(rb"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\Z")


class GitReadError(RuntimeError):
    """Raised when local Git metadata cannot be read safely and completely."""


def discover_repositories(
    paths: Iterable[str | Path],
    *,
    max_depth: int = 4,
    max_repositories: int = 200,
) -> tuple[Path, ...]:
    """Find repository roots below *paths* without entering vendor-sized trees.

    A supplied repository is returned directly. A supplied parent directory is
    searched breadth-first, where the supplied directory has depth zero and its
    immediate children have depth one. Child symlinks are not followed.
    """

    _validate_nonnegative_int("max_depth", max_depth)
    _validate_positive_int("max_repositories", max_repositories)

    roots = [_resolve_directory(path, purpose="discovery path") for path in paths]
    roots.sort(key=_path_sort_key)

    repositories: dict[Path, None] = {}
    visited: set[Path] = set()

    for root in roots:
        pending: deque[tuple[Path, int]] = deque([(root, 0)])
        while pending:
            current, depth = pending.popleft()
            try:
                canonical = current.resolve(strict=True)
            except OSError as exc:
                raise GitReadError(f"Cannot resolve discovery path: {current}") from exc

            if canonical in visited:
                continue
            visited.add(canonical)

            if _looks_like_repository(canonical):
                repositories[canonical] = None
                if len(repositories) > max_repositories:
                    raise GitReadError(f"Repository limit exceeded (maximum {max_repositories})")
                # Do not walk a repository's .git directory or nested vendor checkouts.
                continue

            if depth >= max_depth:
                continue

            try:
                children = sorted(canonical.iterdir(), key=lambda item: _path_sort_key(item))
            except OSError as exc:
                raise GitReadError(f"Cannot read directory during discovery: {canonical}") from exc

            for child in children:
                if _skip_child_directory(child):
                    continue
                try:
                    if child.is_dir():
                        pending.append((child, depth + 1))
                except OSError as exc:
                    raise GitReadError(f"Cannot inspect path during discovery: {child}") from exc

    return tuple(sorted(repositories, key=_path_sort_key))


def load_releases(
    repositories: Iterable[Path],
    *,
    since: date | None = None,
    max_releases: int = 5000,
    timeout_seconds: float = 10.0,
) -> tuple[ReleaseEvent, ...]:
    """Load annotated and lightweight tag dates from local repositories.

    ``timeout_seconds`` is a wall-clock budget for the complete operation, not a
    fresh budget for every repository. The release limit is global and applies
    after the inclusive ``since`` filter.
    """

    if since is not None and (not isinstance(since, date) or isinstance(since, datetime)):
        raise TypeError("since must be a date or None")
    _validate_positive_int("max_releases", max_releases)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number")

    canonical_repositories = {
        _resolve_directory(repository, purpose="repository") for repository in repositories
    }
    ordered_repositories = sorted(canonical_repositories, key=_path_sort_key)

    deadline = time.monotonic() + float(timeout_seconds)
    events: list[ReleaseEvent] = []

    for repository in ordered_repositories:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise GitReadError("Git read timed out")

        remaining_releases = max_releases - len(events)
        # One extra record lets us reject overflow instead of silently truncating.
        output = _read_tag_output(
            repository,
            count=remaining_releases + 1,
            timeout_seconds=remaining_time,
        )
        parsed = _parse_tag_output(output, repository)
        relevant = [event for event in parsed if since is None or event.day >= since]

        if len(relevant) > remaining_releases:
            raise GitReadError(f"Release limit exceeded (maximum {max_releases})")
        events.extend(relevant)

    events.sort(
        key=lambda event: (
            event.released_at,
            event.repository.casefold(),
            event.repository,
            event.tag,
        )
    )
    return tuple(events)


def _read_tag_output(repository: Path, *, count: int, timeout_seconds: float) -> bytes:
    command = [
        _GIT_EXECUTABLE,
        "--no-pager",
        "-c",
        "core.hooksPath=",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        "-C",
        os.fspath(repository),
        "for-each-ref",
        f"--count={count}",
        "--sort=refname",
        "--sort=-creatordate",
        "--format=%(refname:strip=2)%00%(creatordate:iso-strict)",
        "refs/tags",
    ]
    return _run_git_command(command, repository, timeout_seconds=timeout_seconds)


def _run_git_command(command: list[str], repository: Path, *, timeout_seconds: float) -> bytes:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            close_fds=True,
            # Git can launch helpers that inherit its output pipes. A dedicated
            # process group lets timeout and output-limit handling stop the
            # complete tree instead of leaving a child holding a pipe open.
            start_new_session=os.name != "nt",
        )
    except FileNotFoundError as exc:
        raise GitReadError("Git executable was not found") from exc
    except OSError as exc:
        raise GitReadError(
            f"Could not start Git for repository {_sanitize_display_name(repository.name)!r}"
        ) from exc

    assert process.stdout is not None
    assert process.stderr is not None

    overflow = threading.Event()
    overflow_streams: list[str] = []
    reader_errors: list[BaseException] = []
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []

    stdout_thread = threading.Thread(
        target=_read_stream_limited,
        args=(
            process.stdout,
            _MAX_GIT_STDOUT_BYTES,
            stdout_parts,
            overflow,
            overflow_streams,
            "stdout",
            reader_errors,
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stream_limited,
        args=(
            process.stderr,
            _MAX_GIT_STDERR_BYTES,
            stderr_parts,
            overflow,
            overflow_streams,
            "stderr",
            reader_errors,
        ),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while process.poll() is None:
        if overflow.is_set():
            _stop_process(process)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _stop_process(process)
            break
        overflow.wait(min(0.02, remaining))

    # A process may exit just as the output reader notices an oversized pipe.
    stdout_thread.join(timeout=1.0)
    stderr_thread.join(timeout=1.0)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _stop_process(process)
        raise GitReadError("Git output readers did not terminate safely")

    if timed_out:
        raise GitReadError(
            f"Git read timed out for repository {_sanitize_display_name(repository.name)!r}"
        )
    if overflow_streams:
        stream = overflow_streams[0]
        raise GitReadError(f"Git {stream} exceeded the safety limit")
    if reader_errors:
        raise GitReadError("Could not read Git output safely") from reader_errors[0]

    return_code = process.wait()
    stderr = b"".join(stderr_parts)
    if return_code != 0:
        detail = _sanitize_error(stderr)
        suffix = f": {detail}" if detail else ""
        raise GitReadError(
            f"Git could not read repository {_sanitize_display_name(repository.name)!r}{suffix}"
        )

    return b"".join(stdout_parts)


def _read_stream_limited(
    stream: IO[bytes],
    limit: int,
    parts: list[bytes],
    overflow: threading.Event,
    overflow_streams: list[str],
    stream_name: str,
    reader_errors: list[BaseException],
) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            allowed = max(0, limit + 1 - total)
            if allowed:
                parts.append(chunk[:allowed])
            total += len(chunk)
            if total > limit:
                overflow_streams.append(stream_name)
                overflow.set()
                return
    except BaseException as exc:  # pragma: no cover - platform-level pipe failure
        reader_errors.append(exc)
        overflow.set()
    finally:
        stream.close()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            # Fall back to stopping the direct process below.
            with suppress(OSError):
                process.terminate()
    elif process.poll() is None:
        with suppress(OSError):
            process.terminate()

    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            with suppress(ProcessLookupError, OSError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with suppress(OSError):
                process.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=1.0)
    except OSError:
        pass


def _parse_tag_output(output: bytes, repository: Path) -> tuple[ReleaseEvent, ...]:
    repository_name = _sanitize_display_name(repository.name)
    events: list[ReleaseEvent] = []
    records = output.split(b"\n")
    if records and records[-1] == b"":
        records.pop()

    for record_number, record in enumerate(records, start=1):
        if record.endswith(b"\r"):
            record = record[:-1]
        fields = record.split(b"\0")
        if len(fields) != 2 or not fields[0]:
            raise GitReadError(
                f"Malformed tag metadata in repository {repository_name!r} (record {record_number})"
            )

        tag = _sanitize_display_name(fields[0].decode("utf-8", errors="surrogateescape"))
        raw_date = fields[1]
        if _ISO_STRICT_DATE.fullmatch(raw_date) is None:
            raise GitReadError(
                f"Tag {tag!r} in repository {repository_name!r} has no strict ISO creator date"
            )
        try:
            date_text = raw_date.decode("ascii")
            # Python 3.10 does not accept ISO's UTC ``Z`` suffix.
            released_at = datetime.fromisoformat(
                f"{date_text[:-1]}+00:00" if date_text.endswith("Z") else date_text
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise GitReadError(
                f"Tag {tag!r} in repository {repository_name!r} has an invalid creator date"
            ) from exc
        if released_at.tzinfo is None or released_at.utcoffset() is None:
            raise GitReadError(
                f"Tag {tag!r} in repository {repository_name!r} has a timezone-free creator date"
            )

        events.append(ReleaseEvent(repository=repository_name, tag=tag, released_at=released_at))

    return tuple(events)


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "PAGER": "cat",
        }
    )
    return environment


def _resolve_directory(path: str | Path, *, purpose: str) -> Path:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitReadError(f"{purpose.capitalize()} does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise GitReadError(f"{purpose.capitalize()} is not a directory: {candidate}")
    return resolved


def _looks_like_repository(path: Path) -> bool:
    marker = path / ".git"
    if marker.is_dir() or marker.is_file():
        return True
    # Bare repositories have no .git marker.
    return (
        (path / "HEAD").is_file()
        and (path / "objects").is_dir()
        and ((path / "refs").is_dir() or (path / "packed-refs").is_file())
    )


def _skip_child_directory(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    if name.casefold() in _SKIPPED_DIRECTORY_NAMES:
        return True
    try:
        return path.is_symlink()
    except OSError:
        return True


def _sanitize_display_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    safe_characters = [
        " " if character.isspace() or unicodedata.category(character).startswith("C") else character
        for character in normalized
    ]
    compact = " ".join("".join(safe_characters).split())
    if not compact:
        return "repository"
    if len(compact) > _MAX_DISPLAY_NAME_CHARS:
        return f"{compact[: _MAX_DISPLAY_NAME_CHARS - 1]}…"
    return compact


def _sanitize_error(value: bytes) -> str:
    decoded = value.decode("utf-8", errors="replace")
    cleaned = _sanitize_display_name(decoded)
    if cleaned == "repository" and not decoded.strip():
        return ""
    return cleaned[:500]


def _path_sort_key(path: Path) -> tuple[str, str]:
    value = os.fspath(path)
    return (value.casefold(), value)


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _validate_nonnegative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


__all__ = ["GitReadError", "discover_repositories", "load_releases"]
