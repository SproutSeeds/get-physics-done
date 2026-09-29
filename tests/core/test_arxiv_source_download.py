from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from gpd.core.arxiv_source_download import (
    ARXIV_PROJECT_LOCAL_CACHE_DIRNAME,
    ARXIV_SOURCE_MAX_BYTES,
    ARXIV_SOURCE_STORAGE_ENV_VAR,
    ArxivSourceDownload,
    arxiv_source_user_agent,
    build_source_download_url,
    default_arxiv_source_storage_path,
    download_arxiv_source_archive,
    normalize_arxiv_id,
    resolve_default_arxiv_storage_path,
    resolve_source_storage_dir,
)
from gpd.version import resolve_active_version


def test_arxiv_default_storage_path_constant_is_not_exported() -> None:
    import gpd.core.arxiv_source_download as module

    assert not hasattr(module, "ARXIV_DEFAULT_STORAGE_PATH")
    assert "ARXIV_DEFAULT_STORAGE_PATH" not in module.__all__


class _FakeResponse:
    def __init__(self, payload: bytes, *, headers: dict[str, str]) -> None:
        self._buffer = io.BytesIO(payload)
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_normalize_arxiv_id_accepts_known_forms() -> None:
    assert normalize_arxiv_id("2401.12345") == "2401.12345"
    assert normalize_arxiv_id("arXiv:2401.12345v2") == "2401.12345v2"
    assert normalize_arxiv_id("https://arxiv.org/abs/hep-th/9901001") == "hep-th/9901001"
    assert normalize_arxiv_id("https://arxiv.org/abs/2401.12345?context=math#references") == "2401.12345"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2401.12345v2.pdf?download=1") == "2401.12345v2"
    assert normalize_arxiv_id("https://arxiv.org/pdf/hep-th/9901001.pdf#page=1") == "hep-th/9901001"
    assert normalize_arxiv_id("https://arxiv.org/e-print/2401.12345v2") == "2401.12345v2"


def test_normalize_arxiv_id_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="Invalid arXiv ID format"):
        normalize_arxiv_id("not-a-paper")


def test_build_source_download_url_normalizes_first() -> None:
    assert build_source_download_url(" arXiv:2401.12345 ") == "https://arxiv.org/e-print/2401.12345"


def test_arxiv_source_user_agent_tracks_package_version() -> None:
    assert arxiv_source_user_agent() == (
        f"get-physics-done/{resolve_active_version()} (https://github.com/SproutSeeds/get-physics-done)"
    )


def test_resolve_source_storage_dir_uses_sources_subdirectory(tmp_path: Path) -> None:
    resolved = resolve_source_storage_dir(tmp_path / "papers")
    assert resolved == (tmp_path / "papers" / "sources").resolve()
    assert resolved.is_dir()


def test_default_source_storage_path_uses_current_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr("gpd.core.arxiv_source_download.Path.home", lambda: home)

    assert default_arxiv_source_storage_path() == home / ".arxiv-mcp-server" / "papers"
    resolved = resolve_source_storage_dir()

    assert resolved == (home / ".arxiv-mcp-server" / "papers" / "sources").resolve()
    assert resolved.is_dir()


def test_download_arxiv_source_archive_streams_to_disk(tmp_path: Path) -> None:
    payload = b"PK\x03\x04zip-archive-contents"
    response = _FakeResponse(
        payload,
        headers={
            "Content-Type": "application/zip",
            "Content-Length": str(len(payload)),
            "Content-Disposition": 'attachment; filename="2401.12345-source.zip"',
        },
    )

    with patch("gpd.core.arxiv_source_download.urlopen", return_value=response) as mocked_urlopen:
        result = download_arxiv_source_archive("2401.12345", storage_path=tmp_path)

    assert isinstance(result, ArxivSourceDownload)
    assert result.arxiv_id == "2401.12345"
    assert result.filename.endswith(".zip")
    assert result.path.read_bytes() == payload
    assert result.size_bytes == len(payload)
    assert result.cached is False
    request = mocked_urlopen.call_args.args[0]
    assert request.headers["User-agent"] == arxiv_source_user_agent()


def test_download_arxiv_source_archive_uses_existing_file_when_present(tmp_path: Path) -> None:
    payload = b"\x1f\x8bpretend-gzip"
    response = _FakeResponse(
        payload,
        headers={
            "Content-Type": "application/gzip",
            "Content-Length": str(len(payload)),
        },
    )

    with patch("gpd.core.arxiv_source_download.urlopen", return_value=response):
        first = download_arxiv_source_archive("2401.12345", storage_path=tmp_path)

    with patch("gpd.core.arxiv_source_download.urlopen") as second_urlopen:
        second = download_arxiv_source_archive("2401.12345", storage_path=tmp_path, overwrite=False)

    assert first.path == second.path
    assert second.cached is True
    second_urlopen.assert_not_called()


def test_download_arxiv_source_archive_names_content_disposition_by_arxiv_id(tmp_path: Path) -> None:
    payload_a = b"\x1f\x8bpaper-a"
    payload_b = b"\x1f\x8bpaper-b"
    headers = {
        "Content-Type": "application/gzip",
        "Content-Disposition": 'attachment; filename="source.tar.gz"',
    }

    with patch(
        "gpd.core.arxiv_source_download.urlopen",
        side_effect=[
            _FakeResponse(payload_a, headers=headers),
            _FakeResponse(payload_b, headers=headers),
        ],
    ) as mocked_urlopen:
        first = download_arxiv_source_archive("2401.11111", storage_path=tmp_path)
        second = download_arxiv_source_archive("2401.22222", storage_path=tmp_path)

    assert mocked_urlopen.call_count == 2
    assert first.path != second.path
    assert first.path.name == "2401.11111-source.tar.gz"
    assert second.path.name == "2401.22222-source.tar.gz"
    assert first.cached is False
    assert second.cached is False
    assert first.path.read_bytes() == payload_a
    assert second.path.read_bytes() == payload_b


def test_download_arxiv_source_archive_uses_existing_candidate_before_urlopen(tmp_path: Path) -> None:
    storage_dir = resolve_source_storage_dir(tmp_path)
    cached_path = storage_dir / "2401.12345-source.tar.gz"
    cached_path.write_bytes(b"cached-source")

    with patch("gpd.core.arxiv_source_download.urlopen") as mocked_urlopen:
        result = download_arxiv_source_archive("2401.12345", storage_path=tmp_path, overwrite=False)

    mocked_urlopen.assert_not_called()
    assert result.cached is True
    assert result.path == cached_path
    assert result.size_bytes == len(b"cached-source")
    assert result.content_type is None


def test_download_arxiv_source_archive_ignores_non_archive_cache_lookalikes(tmp_path: Path) -> None:
    storage_dir = resolve_source_storage_dir(tmp_path)
    (storage_dir / "2401.12345-source.txt").write_text("not an archive\n", encoding="utf-8")
    (storage_dir / "2401.12345-source.zip").write_bytes(b"")
    payload = b"\x1f\x8bpretend-gzip"
    response = _FakeResponse(
        payload,
        headers={
            "Content-Type": "application/gzip",
            "Content-Length": str(len(payload)),
        },
    )

    with patch("gpd.core.arxiv_source_download.urlopen", return_value=response) as mocked_urlopen:
        result = download_arxiv_source_archive("2401.12345", storage_path=tmp_path, overwrite=False)

    mocked_urlopen.assert_called_once()
    assert result.cached is False
    assert result.path.name == "2401.12345-source.gz"


def test_download_arxiv_source_archive_replaces_empty_deterministic_target(tmp_path: Path) -> None:
    storage_dir = resolve_source_storage_dir(tmp_path)
    empty_target = storage_dir / "2401.12345-source.gz"
    empty_target.write_bytes(b"")
    payload = b"\x1f\x8bpretend-gzip"
    response = _FakeResponse(
        payload,
        headers={
            "Content-Type": "application/gzip",
            "Content-Length": str(len(payload)),
        },
    )

    with patch("gpd.core.arxiv_source_download.urlopen", return_value=response) as mocked_urlopen:
        result = download_arxiv_source_archive("2401.12345", storage_path=tmp_path, overwrite=False)

    mocked_urlopen.assert_called_once()
    assert result.cached is False
    assert result.path == empty_target
    assert result.path.read_bytes() == payload
    assert result.size_bytes == len(payload)


def test_download_arxiv_source_archive_rejects_large_content_length(tmp_path: Path) -> None:
    response = _FakeResponse(
        b"",
        headers={"Content-Length": str(ARXIV_SOURCE_MAX_BYTES + 1)},
    )

    with patch("gpd.core.arxiv_source_download.urlopen", return_value=response):
        with pytest.raises(ConnectionError, match="exceeds size limit"):
            download_arxiv_source_archive("2401.12345", storage_path=tmp_path)


def test_download_arxiv_source_archive_rejects_empty_response(tmp_path: Path) -> None:
    response = _FakeResponse(b"", headers={"Content-Length": "0"})

    with patch("gpd.core.arxiv_source_download.urlopen", return_value=response):
        with pytest.raises(ConnectionError, match="is empty"):
            download_arxiv_source_archive("2401.12345", storage_path=tmp_path)

    storage_dir = resolve_source_storage_dir(tmp_path)
    assert list(storage_dir.iterdir()) == []


def test_download_arxiv_source_archive_rejects_streams_that_grow_too_large(tmp_path: Path) -> None:
    payload = b"a" * (ARXIV_SOURCE_MAX_BYTES + 1)
    response = _FakeResponse(payload, headers={})

    with patch("gpd.core.arxiv_source_download.urlopen", return_value=response):
        with pytest.raises(ConnectionError, match="exceeds size limit"):
            download_arxiv_source_archive("2401.12345", storage_path=tmp_path)


def _make_verified_gpd_project(root: Path) -> Path:
    """Create the minimum set of markers that ``resolve_project_root`` accepts."""

    gpd_dir = root / "GPD"
    gpd_dir.mkdir(parents=True, exist_ok=True)
    (gpd_dir / "state.json").write_text("{}", encoding="utf-8")
    (gpd_dir / "PROJECT.md").write_text("# project\n", encoding="utf-8")
    return root


def test_resolve_default_storage_path_honors_env_var_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "override-cache"
    monkeypatch.setenv(ARXIV_SOURCE_STORAGE_ENV_VAR, str(override))

    # Even when called from inside a verified GPD project, the env var wins.
    project = _make_verified_gpd_project(tmp_path / "proj")
    monkeypatch.chdir(project)

    resolved = resolve_default_arxiv_storage_path()

    assert resolved == override
    # Sanity: env var override is independent of the legacy home cache.
    assert resolved != default_arxiv_source_storage_path()


def test_resolve_default_storage_path_uses_project_local_cache_when_inside_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ARXIV_SOURCE_STORAGE_ENV_VAR, raising=False)
    project = _make_verified_gpd_project(tmp_path / "physics-paper")

    resolved = resolve_default_arxiv_storage_path(project)

    assert resolved == project / ARXIV_PROJECT_LOCAL_CACHE_DIRNAME


def test_resolve_default_storage_path_falls_back_to_home_outside_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ARXIV_SOURCE_STORAGE_ENV_VAR, raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr("gpd.core.arxiv_source_download.Path.home", lambda: home)

    bare = tmp_path / "no-project-here"
    bare.mkdir()

    resolved = resolve_default_arxiv_storage_path(bare)

    assert resolved == home / ".arxiv-mcp-server" / "papers"


def test_resolve_default_storage_path_ignores_blank_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ARXIV_SOURCE_STORAGE_ENV_VAR, "   ")
    home = tmp_path / "home"
    monkeypatch.setattr("gpd.core.arxiv_source_download.Path.home", lambda: home)

    bare = tmp_path / "no-project-here"
    bare.mkdir()

    resolved = resolve_default_arxiv_storage_path(bare)

    assert resolved == home / ".arxiv-mcp-server" / "papers"
