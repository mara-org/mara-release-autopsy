from __future__ import annotations

import os
import subprocess
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import mara_autopsy.git_reader as git_reader
from mara_autopsy.git_reader import GitReadError, discover_repositories, load_releases


def _git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> str:
    env = os.environ.copy()
    if environment:
        env.update(environment)
    result = subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        capture_output=True,
        check=True,
        env=env,
        text=True,
    )
    return result.stdout.strip()


def _init_repository(path: Path, *, commit_date: str = "2024-01-02T03:04:05+00:00") -> Path:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", os.fspath(path)],
        capture_output=True,
        check=True,
    )
    _git(path, "config", "user.name", "Mara Test")
    _git(path, "config", "user.email", "mara@example.invalid")
    (path / "README.md").write_text("test repository\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(
        path,
        "commit",
        "--quiet",
        "-m",
        "initial",
        environment={"GIT_AUTHOR_DATE": commit_date, "GIT_COMMITTER_DATE": commit_date},
    )
    return path.resolve()


def _commit(repository: Path, name: str, *, commit_date: str) -> None:
    (repository / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repository, "add", name)
    _git(
        repository,
        "commit",
        "--quiet",
        "-m",
        name,
        environment={"GIT_AUTHOR_DATE": commit_date, "GIT_COMMITTER_DATE": commit_date},
    )


def test_discover_repository_path_parent_depth_and_skips(tmp_path: Path) -> None:
    alpha = _init_repository(tmp_path / "alpha")
    beta = _init_repository(tmp_path / "group" / "beta")
    _init_repository(tmp_path / "group" / "deeper" / "too-deep")
    _init_repository(tmp_path / "node_modules" / "vendored")
    _init_repository(tmp_path / ".hidden" / "secret")

    discovered = discover_repositories([tmp_path], max_depth=2)

    assert discovered == (alpha, beta)
    assert discover_repositories([alpha], max_depth=0) == (alpha,)
    assert discover_repositories([tmp_path], max_depth=1) == (alpha,)


def test_discover_is_deterministic_deduplicated_and_supports_git_files(
    tmp_path: Path,
) -> None:
    zebra = _init_repository(tmp_path / "zebra")
    alpha = _init_repository(tmp_path / "Alpha")
    worktree_like = tmp_path / "worktree"
    worktree_like.mkdir()
    (worktree_like / ".git").write_text("gitdir: ../zebra/.git\n", encoding="utf-8")

    discovered = discover_repositories([tmp_path, zebra, tmp_path])

    assert discovered == (alpha, worktree_like.resolve(), zebra)


def test_discover_supports_bare_repositories(tmp_path: Path) -> None:
    bare = tmp_path / "archive.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", os.fspath(bare)],
        capture_output=True,
        check=True,
    )

    assert discover_repositories([tmp_path]) == (bare.resolve(),)


def test_discover_rejects_invalid_paths_and_repository_overflow(tmp_path: Path) -> None:
    _init_repository(tmp_path / "one")
    _init_repository(tmp_path / "two")

    with pytest.raises(GitReadError, match="Repository limit exceeded"):
        discover_repositories([tmp_path], max_repositories=1)
    with pytest.raises(GitReadError, match="does not exist"):
        discover_repositories([tmp_path / "missing"])
    with pytest.raises(ValueError, match="max_depth"):
        discover_repositories([tmp_path], max_depth=-1)
    with pytest.raises(ValueError, match="max_repositories"):
        discover_repositories([tmp_path], max_repositories=0)


def test_loads_lightweight_and_annotated_tags_with_creator_dates(tmp_path: Path) -> None:
    repository = _init_repository(tmp_path / "project")
    _git(repository, "tag", "v1.0.0")
    _git(
        repository,
        "tag",
        "--annotate",
        "v2.0.0",
        "--message",
        "release two",
        environment={"GIT_COMMITTER_DATE": "2024-02-03T04:05:06+02:00"},
    )

    releases = load_releases([repository])

    assert [release.tag for release in releases] == ["v1.0.0", "v2.0.0"]
    assert releases[0].released_at == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert releases[1].released_at == datetime(
        2024, 2, 3, 4, 5, 6, tzinfo=timezone(timedelta(hours=2))
    )
    assert {release.repository for release in releases} == {"project"}


def test_since_is_inclusive_and_limit_applies_after_filter(tmp_path: Path) -> None:
    repository = _init_repository(tmp_path / "project", commit_date="2022-01-01T00:00:00+00:00")
    _git(repository, "tag", "old-one")
    _commit(repository, "old-two.txt", commit_date="2023-01-01T00:00:00+00:00")
    _git(repository, "tag", "old-two")
    _commit(repository, "new.txt", commit_date="2024-06-05T23:00:00-03:00")
    _git(repository, "tag", "new")

    releases = load_releases([repository], since=date(2024, 6, 5), max_releases=1)

    assert [release.tag for release in releases] == ["new"]
    assert releases[0].day == date(2024, 6, 5)


def test_release_order_is_deterministic_across_repositories(tmp_path: Path) -> None:
    beta = _init_repository(tmp_path / "beta")
    alpha = _init_repository(tmp_path / "alpha")
    _git(beta, "tag", "same-time")
    _git(alpha, "tag", "same-time")

    releases = load_releases([beta, alpha, beta])

    assert [(release.repository, release.tag) for release in releases] == [
        ("alpha", "same-time"),
        ("beta", "same-time"),
    ]


def test_release_limit_fails_instead_of_truncating(tmp_path: Path) -> None:
    repository = _init_repository(tmp_path / "project")
    for tag in ("one", "two", "three"):
        _git(repository, "tag", tag)

    with pytest.raises(GitReadError, match="Release limit exceeded"):
        load_releases([repository], max_releases=2)


def test_malformed_creator_date_is_rejected(tmp_path: Path) -> None:
    repository = _init_repository(tmp_path / "project")
    blob = _git(repository, "hash-object", "README.md")
    _git(repository, "tag", "blob-tag", blob)

    with pytest.raises(GitReadError, match="strict ISO creator date"):
        load_releases([repository])


def test_missing_git_and_non_repository_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _init_repository(tmp_path / "project")
    not_a_repository = tmp_path / "plain"
    not_a_repository.mkdir()

    with pytest.raises(GitReadError, match="could not read repository"):
        load_releases([not_a_repository])

    monkeypatch.setattr(git_reader, "_GIT_EXECUTABLE", "git-does-not-exist-mara-test")
    with pytest.raises(GitReadError, match="Git executable was not found"):
        load_releases([repository])


def test_output_limit_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _init_repository(tmp_path / "project")
    _git(repository, "tag", "a-long-enough-tag")
    monkeypatch.setattr(git_reader, "_MAX_GIT_STDOUT_BYTES", 8)

    with pytest.raises(GitReadError, match="stdout exceeded"):
        load_releases([repository])


@pytest.mark.skipif(os.name == "nt", reason="test helper uses a POSIX executable script")
def test_timeout_is_hard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _init_repository(tmp_path / "project")
    fake_git = tmp_path / "slow-git"
    fake_git.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setattr(git_reader, "_GIT_EXECUTABLE", os.fspath(fake_git))

    started = __import__("time").monotonic()
    with pytest.raises(GitReadError, match="timed out"):
        load_releases([repository], timeout_seconds=0.05)
    assert __import__("time").monotonic() - started < 1.5


@pytest.mark.skipif(os.name == "nt", reason="control characters are not portable filenames")
def test_repository_display_name_is_sanitized(tmp_path: Path) -> None:
    repository = _init_repository(tmp_path / "unsafe\n\x1b[31mrepo")
    _git(repository, "tag", "v1")

    release = load_releases([repository])[0]

    assert "\n" not in release.repository
    assert "\x1b" not in release.repository
    assert not any(
        unicodedata.category(character).startswith("C") for character in release.repository
    )


def test_tag_display_name_strips_bidirectional_control_characters(tmp_path: Path) -> None:
    repository = _init_repository(tmp_path / "repo")
    deceptive_tag = "release-\u202eliam"
    _git(repository, "tag", deceptive_tag)

    release = load_releases([repository])[0]

    assert release.tag == "release- liam"
    assert "\u202e" not in release.tag


def test_reader_does_not_run_hooks_or_modify_repository(tmp_path: Path) -> None:
    repository = _init_repository(tmp_path / "project")
    _git(repository, "tag", "v1")
    marker = tmp_path / "hook-ran"
    hook = repository / ".git" / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker!s}\n", encoding="utf-8")
    hook.chmod(0o755)
    before = _git(repository, "status", "--porcelain=v1")

    load_releases([repository])

    assert not marker.exists()
    assert _git(repository, "status", "--porcelain=v1") == before


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("max_releases", 0),
        ("max_releases", True),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
    ],
)
def test_load_rejects_invalid_limits(tmp_path: Path, keyword: str, value: object) -> None:
    repository = _init_repository(tmp_path / "project")

    with pytest.raises(ValueError):
        load_releases([repository], **{keyword: value})  # type: ignore[arg-type]


def test_since_rejects_datetime_even_though_it_subclasses_date(tmp_path: Path) -> None:
    repository = _init_repository(tmp_path / "project")

    with pytest.raises(TypeError, match="since"):
        load_releases([repository], since=datetime.now(timezone.utc))  # type: ignore[arg-type]
