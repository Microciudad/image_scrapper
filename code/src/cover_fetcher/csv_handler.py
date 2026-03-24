"""CSV reader / writer with in-place row update and resume support.

Design goals
------------
* Process rows **sequentially** – one at a time.
* **Resumable** – if the process is interrupted the CSV is already updated up
  to the last completed row, so re-running starts from where we left off.
* A row is considered *done* when the ``image_file`` column is non-empty **and**
  the referenced file exists on disk.  Otherwise the row is (re-)processed.
* All changes are written back to the **same** CSV file (in-place).
"""

from __future__ import annotations

import csv
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# The name of the column we add / update
IMAGE_COLUMN = "image_file"

# Characters not allowed in file-system names
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')
_CSV_DELIMITERS = ",;\t|"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def sanitize_filename(text: str) -> str:
    """Replace unsafe filename characters with underscores and collapse spaces."""
    safe = _UNSAFE_CHARS.sub("_", text)
    # Collapse multiple whitespace / underscores
    safe = re.sub(r"[\s_]+", "_", safe).strip("_")
    return safe


def build_image_filename(artist: str, title: str, format_: str) -> str:
    """Return the standardised image filename for a record.

    Pattern: ``{artist}_{title}_{format}.jpg``
    All components are sanitised so the result is always a valid filename.
    """
    parts = [sanitize_filename(p) for p in [artist, title, format_] if p and p.strip()]
    return "_".join(parts) + ".jpg"


def reserve_unique_filename(
    filename: str,
    output_dir: str | Path,
    reserved_names: set[str],
) -> str:
    """Return a filename unique across output_dir and reserved_names.

    If ``filename`` already exists on disk or was reserved for another pending
    row in the current run, numeric suffixes ``_2``, ``_3``, ... are appended.
    The chosen name is added to ``reserved_names`` before returning.
    """
    output_dir = Path(output_dir)
    path = Path(filename)
    stem = path.stem
    suffix = path.suffix or ".jpg"

    candidate = f"{stem}{suffix}"
    counter = 2
    while candidate in reserved_names or (output_dir / candidate).exists():
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1

    reserved_names.add(candidate)
    return candidate


def iter_pending_rows(
    csv_path: str | Path,
    output_dir: str | Path,
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(row_index, row_dict)`` for every row that still needs processing.

    A row is considered done when:
    1. It has a non-empty ``image_file`` column, **and**
    2. The file ``output_dir / image_file`` exists on disk.

    Args:
        csv_path: Path to the input / output CSV file.
        output_dir: Folder where downloaded images are stored.

    Yields:
        ``(row_index, row_dict)`` tuples (0-based row index, excluding header).
    """
    csv_path = Path(csv_path)
    output_dir = Path(output_dir)
    rows = _read_all_rows(csv_path)

    for idx, row in enumerate(rows):
        image_file = (row.get(IMAGE_COLUMN, "") or "").strip()
        if image_file and (output_dir / image_file).exists():
            logger.debug("Row %d already done (%s) – skipping", idx, image_file)
            continue
        yield idx, row


def update_row(
    csv_path: str | Path,
    row_index: int,
    image_filename: str,
) -> None:
    """Set ``image_file`` on the given row and save the CSV atomically.

    Args:
        csv_path: Path to the CSV file.
        row_index: 0-based data row index (not counting the header).
        image_filename: Value to write into the ``image_file`` column.
    """
    csv_path = Path(csv_path)
    rows = _read_all_rows(csv_path)

    if row_index >= len(rows):
        raise IndexError(f"Row index {row_index} out of range ({len(rows)} rows)")

    rows[row_index][IMAGE_COLUMN] = image_filename
    _write_all_rows(csv_path, rows)
    logger.debug("Updated row %d → %s", row_index, image_filename)


def ensure_image_column(csv_path: str | Path) -> None:
    """Add the ``image_file`` column to the CSV if it is not already present.

    This is a no-op if the column already exists.  It is safe to call on every
    program start before the main loop.
    """
    csv_path = Path(csv_path)
    rows = _read_all_rows(csv_path)
    if rows and IMAGE_COLUMN not in rows[0]:
        for row in rows:
            row.setdefault(IMAGE_COLUMN, "")
        _write_all_rows(csv_path, rows)
        logger.info("Added '%s' column to %s", IMAGE_COLUMN, csv_path)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _read_all_rows(csv_path: Path) -> list[dict[str, Any]]:
    delimiter = _detect_delimiter(csv_path)
    with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter, quotechar='"', escapechar='\\')
        return [dict(row) for row in reader]


def _write_all_rows(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    """Write *rows* back to *csv_path* atomically (temp-file + rename)."""
    if not rows:
        return

    delimiter = _detect_delimiter(csv_path)

    fieldnames = list(rows[0].keys())
    if IMAGE_COLUMN not in fieldnames:
        fieldnames.append(IMAGE_COLUMN)

    # Write to a sibling temp file then rename for atomicity
    dir_ = csv_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".tmp_", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=fieldnames,
                extrasaction="ignore",
                delimiter=delimiter,
                quotechar='"',
                escapechar='\\',
            )
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, csv_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _detect_delimiter(csv_path: Path) -> str:
    """Detect delimiter from CSV content; fallback to comma."""
    with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)

    if not sample:
        return ","

    header = sample.splitlines()[0] if sample.splitlines() else ""
    header_delim_counts = {d: header.count(d) for d in _CSV_DELIMITERS}
    dominant_header_delim = max(header_delim_counts, key=header_delim_counts.get)
    if header_delim_counts[dominant_header_delim] > 0:
        return dominant_header_delim

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=_CSV_DELIMITERS)
        return dialect.delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in _CSV_DELIMITERS}
        delimiter = max(counts, key=counts.get)
        return delimiter if counts[delimiter] > 0 else ","
