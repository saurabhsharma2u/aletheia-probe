# SPDX-License-Identifier: MIT
"""DOAJ (Directory of Open Access Journals) data source (optional user-provided CSV)."""

import asyncio
import csv
import glob
from datetime import datetime
from pathlib import Path
from typing import Any

from ...cache import DataSourceManager
from ...enums import AssessmentType
from ...logging_config import get_detail_logger, get_status_logger
from ...normalizer import input_normalizer
from ...validation import validate_issn
from ..core import DataSource


detail_logger = get_detail_logger()
status_logger = get_status_logger()


# DOAJ has published the journal CSV under both word orderings over time:
# the current export is named "doaj_journalcsv_<date>_<time>_utf8.csv", older
# ones "journalcsv__doaj_<date>_<time>_utf8.csv". Accept either so a freshly
# downloaded file works without being renamed.
CSV_FILENAME_PATTERNS = (
    "doaj_journalcsv_*.csv",
    "journalcsv__doaj_*.csv",
)


class DOAJSource(DataSource):
    """Data source for DOAJ journal list (optional user-provided CSV file).

    The user can download the CSV from https://doaj.org/csv and place it in
    .aletheia-probe/doaj/ in the current working directory. The file name
    should match one of the patterns in :data:`CSV_FILENAME_PATTERNS`.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        """Initialize DOAJ source.

        Args:
            data_dir: Directory to search for DOAJ CSV files.
                     Defaults to .aletheia-probe/doaj/ in current directory
        """
        if data_dir is None:
            data_dir = Path.cwd() / ".aletheia-probe" / "doaj"
        self.data_dir = data_dir
        self.file_path: Path | None = None

    def get_name(self) -> str:
        """Return the source identifier used for cache sync."""
        return "doaj"

    def get_list_type(self) -> AssessmentType:
        """Return assessment classification provided by DOAJ data."""
        return AssessmentType.LEGITIMATE

    def should_update(self) -> bool:
        """Check if we should update (monthly for static file)."""
        if not self._find_doaj_file():
            self.skip_reason = "file_not_found"
            return False

        data_source_manager = DataSourceManager()
        last_update = data_source_manager.get_source_last_updated(self.get_name())
        if last_update is None:
            return True

        # Update monthly
        if (datetime.now() - last_update).days < 30:
            self.skip_reason = "already_up_to_date"
            return False

        return True

    def _find_doaj_file(self) -> bool:
        """Find the most recent DOAJ CSV file in the data directory.

        Returns:
            True if file found, False otherwise
        """
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        matching_files: list[str] = []
        for pattern in CSV_FILENAME_PATTERNS:
            matching_files.extend(glob.glob(str(self.data_dir / pattern)))

        if not matching_files:
            status_logger.info(
                f"    {self.get_name()}: No DOAJ journal list found in {self.data_dir}"
            )
            detail_logger.info(
                f"No DOAJ journal list found in {self.data_dir}. "
                "To use DOAJ data locally, download the CSV from "
                '"https://doaj.org/csv" and place it in this directory. '
                f"Accepted file names: {', '.join(CSV_FILENAME_PATTERNS)}."
            )
            return False

        # Use the most recent file
        self.file_path = Path(
            max(matching_files, key=lambda p: Path(p).stat().st_mtime)
        )
        status_logger.info(
            f"    {self.get_name()}: Found journal list: {self.file_path.name}"
        )
        return True

    def _validate_and_normalize_issn(
        self, issn: str | None, journal_title: str
    ) -> str | None:
        """Validate and normalize ISSN format."""
        if not issn:
            return None

        issn = issn.strip()
        if not issn:
            return None

        # Ensure hyphen format (NNNN-NNNN)
        if len(issn) == 8 and "-" not in issn:
            issn = f"{issn[:4]}-{issn[4:]}"

        if not validate_issn(issn):
            detail_logger.warning(
                f"Invalid ISSN '{issn}' for journal '{journal_title}' - skipping ISSN"
            )
            return None

        return issn

    def _parse_row(self, row: dict[str, str]) -> dict[str, Any] | None:
        """Parse a single CSV row into a journal entry dict."""
        title = row.get("Journal title", "").strip()
        if not title or len(title) < 2:
            return None

        issn_raw = row.get("Journal ISSN (print version)", "").strip() or None
        eissn_raw = row.get("Journal EISSN (online version)", "").strip() or None
        publisher = row.get("Publisher", "").strip() or None
        journal_url = row.get("Journal URL", "").strip() or None
        subjects = row.get("Subjects", "").strip() or None

        issn = self._validate_and_normalize_issn(issn_raw, title)
        eissn = self._validate_and_normalize_issn(eissn_raw, title)

        try:
            normalized_input = input_normalizer.normalize(title)
            normalized_name = (
                normalized_input.normalized_venue.name
                if normalized_input.normalized_venue
                else ""
            )
        except Exception as e:
            detail_logger.debug(f"Failed to normalize DOAJ journal '{title}': {e}")
            return None

        metadata: dict[str, Any] = {}
        if journal_url:
            metadata["journal_url"] = journal_url
        if subjects:
            metadata["subjects"] = subjects

        return {
            "journal_name": title,
            "normalized_name": normalized_name,
            "issn": issn,
            "eissn": eissn,
            "publisher": publisher,
            "metadata": metadata,
        }

    async def fetch_data(self) -> list[dict[str, Any]]:
        """Fetch and parse DOAJ journal data from CSV file."""
        if not self._find_doaj_file():
            return []

        if not self.file_path:
            return []

        status_logger.info(
            f"    {self.get_name()}: Loading journal list from {self.file_path.name}"
        )

        try:
            journals = await asyncio.to_thread(self._parse_csv)
            status_logger.info(
                f"    {self.get_name()}: Processed {len(journals)} journals"
            )
            return journals
        except Exception as e:
            status_logger.error(
                f"    {self.get_name()}: Error loading journal list - {e}"
            )
            return []

    def _parse_csv(self) -> list[dict[str, Any]]:
        """Parse the DOAJ CSV file synchronously (called via asyncio.to_thread)."""
        assert self.file_path is not None
        journals = []
        with open(self.file_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                entry = self._parse_row(row)
                if entry:
                    journals.append(entry)
        return journals
