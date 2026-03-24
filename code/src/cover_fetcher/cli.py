"""Command-line interface for cover-fetcher.

Usage examples
--------------
Fetch covers using the default pipeline and save to the ``downloads/`` folder::

    cover-fetcher --csv collection.csv

Fetch covers with a discogs-targeted query and a custom pipeline::

    cover-fetcher --csv collection.csv \\
        --marketplace "discogs" \\
        --output-dir ./covers \\
        --pipeline pipeline.yaml

Run with verbose logging::

    cover-fetcher --csv collection.csv --verbose
"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from cover_fetcher import scraper
from cover_fetcher import csv_handler
from cover_fetcher import processor

app = typer.Typer(
    name="cover-fetcher",
    help="Fetch music record cover images from Google and process them.",
    add_completion=False,
)

console = Console()

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_DEFAULT_OUTPUT_DIR = Path("downloads")
_DEFAULT_PIPELINE = Path("pipeline.yaml")

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def run(
    csv_path: Optional[Path] = typer.Option(
        None,
        "--csv",
        help="Path to the CSV file containing the music collection.",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    input_image: Optional[Path] = typer.Option(
        None,
        "--input-image",
        help="Process an existing image file through the pipeline (without CSV scraping).",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    output_image: Optional[Path] = typer.Option(
        None,
        "--output-image",
        help="Output path for processed image when using --input-image.",
        file_okay=True,
        dir_okay=False,
        writable=True,
    ),
    marketplace: str = typer.Option(
        "",
        "--marketplace",
        help=(
            "Custom keyword(s) appended to the Google query to target a specific "
            "marketplace or image source, e.g. 'discogs' or 'ebay.com' or 'lastfm'."
        ),
    ),
    output_dir: Path = typer.Option(
        _DEFAULT_OUTPUT_DIR,
        "--output-dir",
        help="Directory where downloaded (and processed) images are saved.",
    ),
    pipeline_config: Optional[Path] = typer.Option(
        None,
        "--pipeline",
        help=(
            "Path to the YAML pipeline configuration file.  "
            f"Defaults to '{_DEFAULT_PIPELINE}' if it exists, otherwise a built-in "
            "pipeline (auto-brightness + auto-saturation) is used."
        ),
    ),
    artist_col: str = typer.Option("Band", "--artist-col", help="CSV column name for artist/band."),
    title_col: str = typer.Option("title", "--title-col", help="CSV column name for title."),
    format_col: str = typer.Option("format", "--format-col", help="CSV column name for format."),
    label_col: str = typer.Option("label", "--label-col", help="CSV column name for label."),
    year_col: str = typer.Option("year", "--year-col", help="CSV column name for year."),
    max_retries: int = typer.Option(3, "--max-retries", help="Number of Google fetch retries per row."),
    retry_delay: float = typer.Option(2.0, "--retry-delay", help="Base delay (seconds) between retries."),
    workers: int = typer.Option(
        1,
        "--workers",
        min=1,
        max=20,
        help="Number of rows to process concurrently. Recommended: 1-4.",
    ),
    dump_google_html: Optional[Path] = typer.Option(
        None,
        "--dump-google-html",
        help=(
            "Write raw Google HTML responses to disk for debugging. "
            "Pass a file path (overwritten per attempt) or a directory "
            "(one HTML file per attempt/query)."
        ),
    ),
    keep_google_html: bool = typer.Option(
        False,
        "--keep-google-mhtml/--no-keep-google-mhtml",
        "--keep-google-html/--no-keep-google-html",
        help=(
            "Save the final Google results page for each successfully fetched image, "
            "named like the image file but with an .mhtml extension."
        ),
    ),
    keep_google_html_only: bool = typer.Option(
        False,
        "--keep-google-html-only/--no-keep-google-html-only",
        help=(
            "Save the final Google results page for each successfully fetched image "
            "as plain .html (no mhtml capture)."
        ),
    ),
    browser_fallback: bool = typer.Option(
        True,
        "--browser-fallback/--no-browser-fallback",
        help="Use a real browser session when Google challenges the HTTP scraper.",
    ),
    browser_fallback_single_lane: bool = typer.Option(
        True,
        "--browser-fallback-single-lane/--no-browser-fallback-single-lane",
        help=(
            "When using multiple workers, allow at most one browser fallback at a time. "
            "Disable to force HTTP-only scraping for concurrent runs."
        ),
    ),
    browser_headless: bool = typer.Option(
        True,
        "--browser-headless/--browser-visible",
        help="Run browser fallback headlessly or with a visible browser window.",
    ),
    captcha_wait_timeout: int = typer.Option(
        600,
        "--captcha-wait-timeout",
        help="Seconds to wait for manual CAPTCHA solving in browser before giving up (default 10 min).",
    ),
    reuse_browser_session: bool = typer.Option(
        True,
        "--reuse-browser-session/--fresh-browser-session",
        help="Reuse one browser session across rows to keep cookies and reduce CAPTCHA frequency.",
    ),
    captcha_cooldown_base: int = typer.Option(
        5,
        "--captcha-cooldown-base",
        min=1,
        help="Base cooldown in seconds after each CAPTCHA/block event.",
    ),
    captcha_cooldown_max: int = typer.Option(
        25,
        "--captcha-cooldown-max",
        min=1,
        help="Maximum adaptive cooldown in seconds after repeated CAPTCHA/block events.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Process CSV covers or apply pipeline to a single existing image."""

    _configure_logging(verbose)
    log = logging.getLogger(__name__)

    # -- Image-only processing mode ----------------------------------------
    image_mode = input_image is not None or output_image is not None
    if image_mode:
        if input_image is None or output_image is None:
            raise typer.BadParameter("--input-image and --output-image must be used together.")

        pipeline_steps = _resolve_pipeline(pipeline_config)

        try:
            processed_bytes = processor.apply_pipeline(input_image.read_bytes(), pipeline_steps)
        except (ValueError, OSError, RuntimeError) as exc:
            raise typer.BadParameter(f"Could not process image {input_image}: {exc}") from exc

        output_image.parent.mkdir(parents=True, exist_ok=True)
        output_image.write_bytes(processed_bytes)
        console.print(f"[green]✓ Processed image saved:[/] {output_image}")
        return

    if csv_path is None:
        raise typer.BadParameter("--csv is required unless using --input-image/--output-image mode.")

    # -- Ensure output directory exists -------------------------------------
    output_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold green]Output directory:[/] {output_dir.resolve()}")

    # -- Load image pipeline ------------------------------------------------
    pipeline_steps = _resolve_pipeline(pipeline_config)

    # -- Prepare CSV --------------------------------------------------------
    csv_handler.ensure_image_column(csv_path)

    # -- Main processing loop -----------------------------------------------
    pending = list(csv_handler.iter_pending_rows(csv_path, output_dir))

    console.print(f"[bold]Rows to process:[/] {len(pending)}")

    effective_browser_fallback = browser_fallback and (workers == 1 or browser_fallback_single_lane)
    effective_reuse_browser_session = reuse_browser_session and effective_browser_fallback and workers == 1
    if workers > 1 and browser_fallback:
        if browser_fallback_single_lane:
            console.print(
                "[yellow]Concurrency enabled:[/] browser fallback is restricted to one guarded lane with a fresh "
                "browser per fallback while HTTP fetching continues in parallel."
            )
        else:
            console.print(
                "[yellow]Concurrency enabled:[/] browser fallback is disabled when using more than one worker."
            )

    reserved_names: set[str] = set()
    jobs: list[dict[str, Any]] = []
    for row_index, row in pending:
        artist, title, year, format_, label = _extract_query_fields(row)
        filename = csv_handler.reserve_unique_filename(
            csv_handler.build_image_filename(artist, title, format_),
            output_dir,
            reserved_names,
        )
        dest = output_dir / filename
        final_google_html_path: Optional[Path] = None
        final_google_html_format = "mhtml"
        if keep_google_html_only:
            final_google_html_path = dest.with_suffix(".html")
            final_google_html_format = "html"
        elif keep_google_html:
            final_google_html_path = dest.with_suffix(".mhtml")
            final_google_html_format = "mhtml"

        jobs.append(
            {
                "row_index": row_index,
                "artist": artist,
                "title": title,
                "year": year,
                "format": format_,
                "label": label,
                "filename": filename,
                "dest": dest,
                "final_google_html_path": final_google_html_path,
                "final_google_html_format": final_google_html_format,
            }
        )

    def _process_job(job: dict[str, Any]) -> dict[str, Any]:
        temp_image_path = _create_staging_path(output_dir, Path(job["dest"]).suffix)
        temp_html_path = (
            _create_staging_path(output_dir, Path(job["final_google_html_path"]).suffix)
            if job["final_google_html_path"] is not None
            else None
        )

        query = scraper.build_query(job["artist"], job["title"], job["format"], job["label"], job["year"], marketplace)
        log.debug("Query: %r", query)

        image_bytes = scraper.fetch_cover(
            query,
            max_retries=max_retries,
            retry_delay=retry_delay,
            dump_html_path=dump_google_html,
            browser_fallback=effective_browser_fallback,
            browser_headless=browser_headless,
            captcha_wait_seconds=captcha_wait_timeout,
            reuse_browser_session=effective_reuse_browser_session,
            captcha_cooldown_base_seconds=captcha_cooldown_base,
            captcha_cooldown_max_seconds=captcha_cooldown_max,
            final_html_path=temp_html_path,
            final_html_format=job["final_google_html_format"],
        )

        if image_bytes is None:
            if temp_image_path.exists():
                temp_image_path.unlink(missing_ok=True)
            if temp_html_path is not None and temp_html_path.exists():
                temp_html_path.unlink(missing_ok=True)
            return {"status": "not_found", **job}

        try:
            processed_bytes = processor.apply_pipeline(image_bytes, pipeline_steps)
        except (ValueError, OSError, RuntimeError) as exc:
            log.warning("Pipeline failed for %r – saving raw image. Error: %s", job["title"], exc)
            processed_bytes = image_bytes

        temp_image_path.write_bytes(processed_bytes)
        return {
            "status": "saved",
            "temp_image_path": temp_image_path,
            "temp_html_path": temp_html_path,
            **job,
        }

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=False,
        ) as progress:
            task_id = progress.add_task(f"Fetching covers with {workers} worker(s)…", total=len(jobs))

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(_process_job, job): job for job in jobs}

                for future in as_completed(future_map):
                    job = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        log.exception("Worker failed for row %s: %s", job["row_index"], exc)
                        console.print(f"  [red]✗ Worker error:[/] {job['artist']} – {job['title']}")
                        progress.advance(task_id)
                        continue

                    if result["status"] == "saved":
                        try:
                            csv_handler.update_row(csv_path, result["row_index"], result["filename"])
                            _finalize_staged_result(result)
                            console.print(f"  [green]✓[/] {result['filename']}")
                        except Exception as exc:
                            log.exception("Finalize failed for %s: %s", result["filename"], exc)
                            try:
                                csv_handler.update_row(csv_path, result["row_index"], "")
                            except Exception:
                                log.exception("Rollback failed for row %s", result["row_index"])
                            _cleanup_staged_result(result)
                            console.print(f"  [red]✗ Finalize error:[/] {result['artist']} – {result['title']}")
                    else:
                        console.print(f"  [yellow]⚠ No image found for:[/] {result['artist']} – {result['title']}")

                    progress.advance(task_id)
    finally:
        scraper.close_browser_session()

    console.print("[bold green]Done.[/]")


# ---------------------------------------------------------------------------
# Helper: pipeline resolution
# ---------------------------------------------------------------------------


def _resolve_pipeline(pipeline_config: Optional[Path]) -> list:
    """Return pipeline steps from config file or built-in defaults."""
    if pipeline_config is not None:
        console.print(f"[bold]Pipeline:[/] {pipeline_config}")
        return processor.load_pipeline(pipeline_config)

    if _DEFAULT_PIPELINE.exists():
        console.print(f"[bold]Pipeline:[/] {_DEFAULT_PIPELINE} (auto-detected)")
        return processor.load_pipeline(_DEFAULT_PIPELINE)

    console.print("[bold]Pipeline:[/] built-in defaults (auto-brightness + auto-saturation)")
    return processor.DEFAULT_PIPELINE


def _create_staging_path(output_dir: Path, suffix: str) -> Path:
    fd, tmp_path = tempfile.mkstemp(dir=output_dir, prefix=".stage_", suffix=suffix)
    os.close(fd)
    return Path(tmp_path)


def _finalize_staged_result(result: dict[str, Any]) -> None:
    temp_image_path = Path(result["temp_image_path"])
    dest = Path(result["dest"])
    os.replace(temp_image_path, dest)
    log = logging.getLogger(__name__)
    log.info("Saved: %s", dest)

    temp_html_path = result.get("temp_html_path")
    final_html_path = result.get("final_google_html_path")
    if temp_html_path is not None and final_html_path is not None and Path(temp_html_path).exists():
        os.replace(Path(temp_html_path), Path(final_html_path))


def _cleanup_staged_result(result: dict[str, Any]) -> None:
    for key in ("temp_image_path", "temp_html_path"):
        path = result.get(key)
        if path is not None:
            Path(path).unlink(missing_ok=True)

    for key in ("dest", "final_google_html_path"):
        path = result.get(key)
        if path is not None:
            Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Helper: logging setup
# ---------------------------------------------------------------------------


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def _row_get(row: dict, column_name: str) -> str:
    """Return row value for *column_name* using case-insensitive header matching."""
    target = (column_name or "").strip().lower()
    for key, value in row.items():
        if (key or "").strip().lower() == target:
            return (value or "").strip()
    return ""


def _extract_query_fields(row: dict) -> tuple[str, str, str, str, str]:
    """Return exactly (Band, Title, year, FORMAT, Label) from *row*.

    If CSV parsing produced a malformed single-field row, fall back to parsing the
    semicolon payload and taking the first relevant 6 columns.
    """
    if _has_expected_headers(row):
        band = _row_get(row, "Band")
        title = _row_get(row, "Title")
        year = _row_get(row, "year")
        format_ = _row_get(row, "FORMAT")
        label = _row_get(row, "Label")
        return (
            _first_segment(band),
            _first_segment(title),
            _first_segment(year),
            _first_segment(format_),
            _first_segment(label),
        )

    payload = _find_semicolon_payload(row)
    if payload:
        columns = _split_semicolon_row(payload)
        if len(columns) >= 6:
            return (
                _first_segment(columns[0]),
                _first_segment(columns[1]),
                _first_segment(columns[3]),
                _first_segment(columns[4]),
                _first_segment(columns[5]),
            )

    # Last-resort fallback (still restricted to expected field names only).
    band = _row_get(row, "Band")
    title = _row_get(row, "Title")
    year = _row_get(row, "year")
    format_ = _row_get(row, "FORMAT")
    label = _row_get(row, "Label")
    return (
        _first_segment(band),
        _first_segment(title),
        _first_segment(year),
        _first_segment(format_),
        _first_segment(label),
    )


def _has_expected_headers(row: dict) -> bool:
    expected = {"band", "title", "year", "format", "label"}
    keys = {(key or "").strip().lower() for key in row.keys()}
    return expected.issubset(keys)


def _find_semicolon_payload(row: dict) -> str:
    """Return the best candidate text that looks like a semicolon CSV row."""
    candidates: list[str] = []
    for text in row.values():
        value = (text or "").strip()
        if value.count(";") >= 5 and any(ch.isalpha() for ch in value):
            candidates.append(value.strip('"'))
    if not candidates:
        return ""
    return max(candidates, key=len)


def _split_semicolon_row(payload: str) -> list[str]:
    """Split one semicolon-delimited CSV payload using CSV quoting rules."""
    try:
        row = next(csv.reader([payload], delimiter=";", quotechar='"'))
    except Exception:
        return []
    return [(value or "").strip() for value in row]


def _first_segment(value: str) -> str:
    """Keep only the first cell-like segment to prevent column leakage."""
    text = (value or "").strip()
    for delim in (";", ","):
        if delim in text:
            text = text.split(delim, 1)[0].strip()
    return text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
