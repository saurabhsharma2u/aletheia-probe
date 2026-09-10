# SPDX-License-Identifier: MIT
"""Tests for DOAJ journal list source file discovery."""

import os
import time
from pathlib import Path

import pytest

from aletheia_probe.enums import AssessmentType
from aletheia_probe.updater.sources.doaj import DOAJSource


@pytest.fixture
def source(tmp_path: Path) -> DOAJSource:
    """Create DOAJ source pointing at an empty temporary data directory."""
    return DOAJSource(data_dir=tmp_path / "doaj")


def _write_csv(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Journal title,Journal ISSN (print version)\n", encoding="utf-8")
    return path


def test_get_name(source: DOAJSource):
    """Test source name."""
    assert source.get_name() == "doaj"


def test_get_list_type(source: DOAJSource):
    """Test source list type."""
    assert source.get_list_type() == AssessmentType.LEGITIMATE


def test_no_file_found(source: DOAJSource):
    """Missing CSV is reported as not found."""
    assert source._find_doaj_file() is False
    assert source.file_path is None


def test_finds_current_doaj_export_name(source: DOAJSource):
    """The current DOAJ export name (doaj_journalcsv_*) is accepted."""
    expected = _write_csv(source.data_dir / "doaj_journalcsv_20260810_2320_utf8.csv")

    assert source._find_doaj_file() is True
    assert source.file_path == expected


def test_finds_legacy_doaj_export_name(source: DOAJSource):
    """The legacy DOAJ export name (journalcsv__doaj_*) is still accepted."""
    expected = _write_csv(source.data_dir / "journalcsv__doaj_20260314_1626_utf8.csv")

    assert source._find_doaj_file() is True
    assert source.file_path == expected


def test_unrelated_csv_is_ignored(source: DOAJSource):
    """A CSV not matching either naming scheme is ignored."""
    _write_csv(source.data_dir / "doaj.csv")

    assert source._find_doaj_file() is False
    assert source.file_path is None


def test_most_recent_file_wins_across_naming_schemes(source: DOAJSource):
    """With both namings present, the most recently modified file is used."""
    older = _write_csv(source.data_dir / "journalcsv__doaj_20260314_1626_utf8.csv")
    newer = _write_csv(source.data_dir / "doaj_journalcsv_20260810_2320_utf8.csv")

    now = time.time()
    os.utime(older, (now - 3600, now - 3600))
    os.utime(newer, (now, now))

    assert source._find_doaj_file() is True
    assert source.file_path == newer


def test_creates_missing_data_dir(source: DOAJSource):
    """A missing data directory is created so users have somewhere to drop the file."""
    assert not source.data_dir.exists()

    source._find_doaj_file()

    assert source.data_dir.is_dir()
