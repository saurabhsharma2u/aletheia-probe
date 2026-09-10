# SPDX-License-Identifier: MIT
"""Tests for DBLP conference source."""

import gzip
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import URLError

import pytest
from defusedxml import ElementTree as DefusedET

from aletheia_probe.enums import AssessmentType
from aletheia_probe.updater.sources.dblp import DblpVenueSource


def _make_source(tmp_path: Path, dump_url: str) -> DblpVenueSource:
    """Create a DBLP source configured with the given dump URL."""
    with patch(
        "aletheia_probe.updater.sources.dblp.get_config_manager"
    ) as mock_config_manager:
        mock_config = Mock()
        mock_config.data_source_urls = Mock()
        mock_config.data_source_urls.dblp_xml_dump_url = dump_url
        mock_config_manager.return_value.load_config.return_value = mock_config

        return DblpVenueSource(
            data_dir=tmp_path / "dblp",
            min_entries_for_series=1,
            min_active_years=1,
            update_interval_days=30,
        )


@pytest.fixture
def source(tmp_path: Path) -> DblpVenueSource:
    """Create DBLP source pointing at dblp.org, which blocks automated downloads."""
    return _make_source(tmp_path, "https://dblp.org/xml/dblp.xml.gz")


@pytest.fixture
def mirror_source(tmp_path: Path) -> DblpVenueSource:
    """Create DBLP source pointing at a mirror that serves the dump directly."""
    return _make_source(tmp_path, "https://mirror.example.org/dblp.xml.gz")


def _write_gz(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(content)


def test_get_name(source: DblpVenueSource):
    """Test source name."""
    assert source.get_name() == "dblp_venues"


def test_get_list_type(source: DblpVenueSource):
    """Test source list type."""
    assert source.get_list_type() == AssessmentType.LEGITIMATE


def test_should_update_when_dump_missing(source: DblpVenueSource):
    """Test update required when local dump file is missing."""
    with patch("aletheia_probe.updater.sources.dblp.DataSourceManager") as manager_cls:
        manager = Mock()
        manager.get_source_last_updated.return_value = datetime.now() - timedelta(
            days=1
        )
        manager_cls.return_value = manager
        assert source.should_update() is True


def test_should_update_recent_update(source: DblpVenueSource):
    """Test update skipped when source is fresh."""
    _write_gz(source.dump_path, "<dblp/>")
    with patch("aletheia_probe.updater.sources.dblp.DataSourceManager") as manager_cls:
        manager = Mock()
        manager.get_source_last_updated.return_value = datetime.now() - timedelta(
            days=2
        )
        manager_cls.return_value = manager
        assert source.should_update() is False


def test_parse_dump_file_extracts_conference_entries(source: DblpVenueSource):
    """Test parsing minimal DBLP XML into conference entries."""
    xml_content = """
<dblp>
  <proceedings key="conf/testconf/2024">
    <title>Proceedings of the Test Conference 2024</title>
    <booktitle>TestConf</booktitle>
    <year>2024</year>
  </proceedings>
  <inproceedings key="conf/testconf/2024/p1">
    <booktitle>TestConf</booktitle>
    <year>2024</year>
  </inproceedings>
  <article key="journals/tosem/Example">
    <title>Example Journal Article</title>
    <journal>ACM Transactions on Software Engineering and Methodology</journal>
    <year>2024</year>
    <issn>1049-331X</issn>
  </article>
</dblp>
"""
    _write_gz(source.dump_path, xml_content)

    journals = source._parse_dump_file()
    assert len(journals) >= 2

    conference_entries = [
        j
        for j in journals
        if j.get("metadata", {}).get("dblp_entry_type") == "conference"
    ]
    assert conference_entries
    first_conf = conference_entries[0]
    assert "journal_name" in first_conf
    assert "normalized_name" in first_conf
    assert first_conf["metadata"]["dblp_series"] == "testconf"
    assert first_conf["metadata"]["dblp_entry_count"] >= 2

    journal_entries = [
        j for j in journals if j.get("metadata", {}).get("dblp_entry_type") == "journal"
    ]
    assert journal_entries
    first_journal = journal_entries[0]
    assert first_journal["metadata"]["dblp_series"] == "tosem"


def test_parse_dump_file_handles_named_xml_entities(source: DblpVenueSource):
    """Test parser supports DBLP named entities like &uuml;."""
    xml_content = """
<dblp>
  <proceedings key="conf/entityconf/2024">
    <title>Proceedings M&uuml;nchen 2024</title>
    <booktitle>EntityConf</booktitle>
    <year>2024</year>
  </proceedings>
</dblp>
"""
    _write_gz(source.dump_path, xml_content)

    journals = source._parse_dump_file()
    assert journals


@pytest.mark.asyncio
async def test_fetch_data_uses_download_and_parse(mirror_source: DblpVenueSource):
    """Test fetch_data downloads from a configured mirror when dump is missing."""
    with (
        patch.object(mirror_source, "_download_dump", new=AsyncMock()) as download_mock,
        patch.object(
            mirror_source,
            "_parse_dump_file",
            return_value=[{"journal_name": "Test", "normalized_name": "test"}],
        ) as parse_mock,
    ):
        result = await mirror_source.fetch_data()
        download_mock.assert_awaited_once()
        parse_mock.assert_called_once_with()
        assert len(result) == 1


@pytest.mark.asyncio
async def test_fetch_data_requires_manual_dump_for_blocked_host(
    source: DblpVenueSource,
):
    """Test no download is attempted for hosts that forbid automated access."""
    with (
        patch.object(source, "_download_dump", new=AsyncMock()) as download_mock,
        patch.object(source, "_parse_dump_file") as parse_mock,
        pytest.raises(FileNotFoundError, match="No local DBLP dump"),
    ):
        await source.fetch_data()

    download_mock.assert_not_awaited()
    parse_mock.assert_not_called()


@pytest.mark.parametrize(
    "dump_url",
    [
        "https://dblp.org/xml/dblp.xml.gz",
        "https://www.dblp.org/xml/dblp.xml.gz",
        "https://dblp.uni-trier.de/xml/dblp.xml.gz",
        "https://dblp.dagstuhl.de/xml/dblp.xml.gz",
    ],
)
def test_requires_manual_download_for_dblp_hosts(tmp_path: Path, dump_url: str):
    """Test dblp.org and its official mirrors are treated as manual-only."""
    assert _make_source(tmp_path, dump_url)._requires_manual_download() is True


def test_mirror_host_allows_automated_download(mirror_source: DblpVenueSource):
    """Test a user-configured mirror is still downloaded automatically."""
    assert mirror_source._requires_manual_download() is False


@pytest.mark.asyncio
async def test_fetch_data_skips_download_when_dump_exists(source: DblpVenueSource):
    """Test fetch_data reuses local dump when it is present and parseable."""
    _write_gz(source.dump_path, "<dblp></dblp>")

    with (
        patch.object(source, "_download_dump", new=AsyncMock()) as download_mock,
        patch.object(
            source,
            "_parse_dump_file",
            return_value=[{"journal_name": "Cached", "normalized_name": "cached"}],
        ) as parse_mock,
    ):
        result = await source.fetch_data()
        download_mock.assert_not_awaited()
        parse_mock.assert_called_once_with()
        assert len(result) == 1


@pytest.mark.asyncio
async def test_fetch_data_reports_invalid_existing_dump_without_retry(
    mirror_source: DblpVenueSource,
):
    """Test an unparseable local dump fails once instead of re-downloading."""
    _write_gz(mirror_source.dump_path, "<dblp><broken>")

    with (
        patch.object(mirror_source, "_download_dump", new=AsyncMock()) as download_mock,
        patch.object(
            mirror_source,
            "_parse_dump_file",
            side_effect=DefusedET.ParseError("invalid xml"),
        ) as parse_mock,
        pytest.raises(ValueError, match="not a readable gzip XML dump"),
    ):
        await mirror_source.fetch_data()

    download_mock.assert_not_awaited()
    assert parse_mock.call_count == 1


class _FakeContent:
    """Minimal stand-in for aiohttp's streaming response body."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        async def generate() -> AsyncIterator[bytes]:
            for chunk in self._chunks:
                yield chunk

        return generate()


class _FakeResponse:
    """Minimal stand-in for an aiohttp response used as async context manager."""

    def __init__(self, chunks: list[bytes], content_type: str) -> None:
        self.content = _FakeContent(chunks)
        self.content_type = content_type

    def raise_for_status(self) -> None:
        return None

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def get(self, _url: str) -> _FakeResponse:
        return self._response

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False


def _patch_download(chunks: list[bytes], content_type: str = "application/gzip") -> Any:
    """Patch the aiohttp session used by the DBLP source with a canned response."""
    return patch(
        "aletheia_probe.updater.sources.dblp.ClientSession",
        return_value=_FakeSession(_FakeResponse(chunks, content_type)),
    )


def _part_files(source: DblpVenueSource) -> list[Path]:
    return list(source.data_dir.glob("dblp-*.part"))


@pytest.mark.asyncio
async def test_download_rejects_html_content_type(mirror_source: DblpVenueSource):
    """Test an HTML response is rejected before any body is written."""
    mirror_source.data_dir.mkdir(parents=True, exist_ok=True)

    with _patch_download([b"<!doctype html><html>"], content_type="text/html"):
        with pytest.raises(URLError, match="instead of a gzip dump"):
            await mirror_source._download_dump()

    assert not mirror_source.dump_path.exists()
    assert _part_files(mirror_source) == []


@pytest.mark.asyncio
async def test_download_rejects_non_gzip_body(mirror_source: DblpVenueSource):
    """Test a non-gzip body is rejected even when the content type looks right."""
    mirror_source.data_dir.mkdir(parents=True, exist_ok=True)

    with _patch_download([b"<!doctype html><html>lied about the type</html>"]):
        with pytest.raises(URLError, match="did not return a gzip file"):
            await mirror_source._download_dump()

    assert not mirror_source.dump_path.exists()
    assert _part_files(mirror_source) == []


@pytest.mark.asyncio
async def test_download_rejects_empty_response(mirror_source: DblpVenueSource):
    """Test an empty response is rejected instead of reported as complete."""
    mirror_source.data_dir.mkdir(parents=True, exist_ok=True)

    with _patch_download([]):
        with pytest.raises(URLError, match="empty response"):
            await mirror_source._download_dump()

    assert not mirror_source.dump_path.exists()
    assert _part_files(mirror_source) == []


@pytest.mark.asyncio
async def test_failed_download_leaves_existing_dump_untouched(
    mirror_source: DblpVenueSource,
):
    """Test a rejected download does not clobber a working local dump."""
    _write_gz(mirror_source.dump_path, "<dblp></dblp>")
    original = mirror_source.dump_path.read_bytes()

    with _patch_download([b"<!doctype html><html>"], content_type="text/html"):
        with pytest.raises(URLError):
            await mirror_source._download_dump()

    assert mirror_source.dump_path.read_bytes() == original
    assert _part_files(mirror_source) == []


@pytest.mark.asyncio
async def test_download_installs_valid_gzip_dump(mirror_source: DblpVenueSource):
    """Test a gzip response is installed at the dump path."""
    mirror_source.data_dir.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(b"<dblp></dblp>")

    with _patch_download([payload[:2], payload[2:]]):
        await mirror_source._download_dump()

    assert mirror_source.dump_path.exists()
    with gzip.open(mirror_source.dump_path, "rb") as f:
        assert f.read() == b"<dblp></dblp>"
    assert _part_files(mirror_source) == []
