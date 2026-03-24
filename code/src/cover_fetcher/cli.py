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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from cover_fetcher import scraper
from cover_fetcher import csv_handler
from cover_fetcher import processor
from cover_fetcher.settings import COUNTRY_EQUIVALENCES

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
_RESULT_UPDATE_BATCH_SIZE = 25
_PENDING_UPDATES_SUFFIX = ".pending_updates.json"

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def run(
    csv_path: Optional[Path] = typer.Option(
        None,
        "--csv",
        help="Path to the collection file (.csv or .xlsx).",
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
    country_col: str = typer.Option(
        "ED",
        "--country-col",
        help="CSV/XLSX column name for edition country/region code (for example ED).",
    ),
    max_retries: int = typer.Option(3, "--max-retries", help="Number of Google fetch retries per row."),
    retry_delay: float = typer.Option(2.0, "--retry-delay", help="Base delay (seconds) between retries."),
    workers: int = typer.Option(
        1,
        "--workers",
        min=1,
        max=20,
        help="Number of rows to process concurrently. Recommended: 1-4.",
    ),
    max_jobs: int = typer.Option(
        0,
        "--max-jobs",
        min=0,
        help="Maximum number of missing-image jobs to process in this run (0 means no limit).",
    ),
    scan_log_every: int = typer.Option(
        200,
        "--scan-log-every",
        min=0,
        help="Log row-scan progress every N rows while reading input (0 disables).",
    ),
    process_log_every: int = typer.Option(
        1,
        "--process-log-every",
        min=0,
        help="Log processed counter every N completed rows (0 disables).",
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
    discogs_hq: bool = typer.Option(
        False,
        "--discogs-hq/--no-discogs-hq",
        help=(
            "Enable deep Discogs mode: prioritize high-resolution Discogs CDN image URLs "
            "from Google Images result payloads and rendered page sources."
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
    captcha_cooldown_range: Optional[str] = typer.Option(
        None,
        "--captcha-cooldown-range",
        help=(
            "Random cooldown range as MIN-MAX seconds (for example: 1-50). "
            "Overrides --captcha-cooldown-base and --captcha-cooldown-max."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Process CSV covers or apply pipeline to a single existing image."""

    _configure_logging(verbose)
    log = logging.getLogger(__name__)
    country_map = COUNTRY_EQUIVALENCES
    resolved_cooldown_base, resolved_cooldown_max = _resolve_captcha_cooldown_bounds(
        captcha_cooldown_range,
        captcha_cooldown_base,
        captcha_cooldown_max,
    )

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

    # -- Prepare collection file --------------------------------------------
    try:
        csv_handler.ensure_image_column(csv_path)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    effective_browser_fallback = browser_fallback and (workers == 1 or browser_fallback_single_lane)
    # Reusing one guarded browser session preserves solved-CAPTCHA cookies even with multi-worker HTTP fetching.
    effective_reuse_browser_session = reuse_browser_session and effective_browser_fallback
    persisted_pending_updates = _load_persisted_pending_updates(csv_path, output_dir, log)
    if persisted_pending_updates:
        recovered_count = len(persisted_pending_updates)
        startup_updates = dict(persisted_pending_updates)
        if _flush_result_updates(csv_path, startup_updates, log, persisted_pending_updates):
            console.print(f"[bold cyan]Recovered deferred spreadsheet rows:[/] {recovered_count}")
        else:
            console.print(
                "[yellow]Spreadsheet still locked:[/] deferred filename updates are preserved and will be retried in this run."
            )

    if workers > 1 and browser_fallback:
        if browser_fallback_single_lane:
            if effective_reuse_browser_session:
                console.print(
                    "[yellow]Concurrency enabled:[/] browser fallback is restricted to one guarded lane, "
                    "reusing a shared browser session while HTTP fetching continues in parallel."
                )
            else:
                console.print(
                    "[yellow]Concurrency enabled:[/] browser fallback is restricted to one guarded lane with a fresh "
                    "browser per fallback while HTTP fetching continues in parallel."
                )
        else:
            console.print(
                "[yellow]Concurrency enabled:[/] browser fallback is disabled when using more than one worker."
            )

    # -- Main processing loop -----------------------------------------------
    console.print("[bold]Scanning input rows for pending work...[/]")
    reserved_names: set[str] = set()
    existing_output_names = {
        p.name
        for p in output_dir.iterdir()
        if p.is_file()
    }
    if existing_output_names:
        console.print(f"[dim]Indexed existing output files:[/] {len(existing_output_names)}")

    jobs: list[dict[str, Any]] = []
    auto_fill_updates: dict[int, str] = {}
    scanned_pending_rows = 0
    prep_log_every = max(500, scan_log_every) if scan_log_every > 0 else 1000
    pending_rows_iter = csv_handler.iter_pending_rows(csv_path, output_dir, scan_log_every=scan_log_every)
    try:
        for row_index, row in pending_rows_iter:
            scanned_pending_rows += 1
            if scanned_pending_rows % prep_log_every == 0:
                console.print(f"[dim]Preparing jobs from pending rows:[/] {scanned_pending_rows} checked")

            artist, title, year, format_, label = _extract_query_fields(row)
            edition_country = _translate_country(_extract_country_value(row, country_col), country_map)
            expected_filename = csv_handler.build_image_filename(artist, title, format_)
            existing_image_value = _get_row_image_value(row)
            persisted_image_value = persisted_pending_updates.get(row_index, "")
            has_meaningful_expected_name = expected_filename != ".jpg" and any(
                field.strip() for field in (artist, title, format_)
            )
            existing_disk_filename = _find_existing_image_filename(existing_output_names, artist, title, format_)

            if (
                not existing_image_value
                and has_meaningful_expected_name
                and existing_disk_filename
            ):
                auto_fill_updates[row_index] = existing_disk_filename
                continue

            if (
                not existing_image_value
                and persisted_image_value
                and (output_dir / persisted_image_value).is_file()
            ):
                auto_fill_updates[row_index] = persisted_image_value
                continue

            filename = csv_handler.reserve_unique_filename(
                expected_filename,
                output_dir,
                reserved_names,
                existing_names=existing_output_names,
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
                    "edition_country": edition_country,
                    "filename": filename,
                    "dest": dest,
                    "final_google_html_path": final_google_html_path,
                    "final_google_html_format": final_google_html_format,
                }
            )

            if max_jobs > 0 and len(jobs) >= max_jobs:
                console.print(f"[bold yellow]Max jobs limit reached:[/] {max_jobs}")
                break
    finally:
        close_iter = getattr(pending_rows_iter, "close", None)
        if callable(close_iter):
            close_iter()

    if auto_fill_updates:
        try:
            csv_handler.update_rows(csv_path, auto_fill_updates)
        except Exception as exc:
            log.warning("Could not persist auto-filled image values in batch: %s", exc)

    console.print(f"[bold]Pending rows inspected:[/] {scanned_pending_rows}")
    console.print(f"[bold]Rows to process:[/] {len(jobs)}")
    if auto_fill_updates:
        console.print(f"[bold cyan]Rows auto-filled from existing files:[/] {len(auto_fill_updates)}")

    def _process_job(job: dict[str, Any]) -> dict[str, Any]:
        artist = job['artist'] or '<unknown artist>'
        title = job['title'] or '<unknown title>'
        worker_label = f"row {job['row_index']}: {artist} - {title}"
        log.info(
            "Worker starting row %s: %s - %s",
            job["row_index"],
            job["artist"] or "<unknown artist>",
            job["title"] or "<unknown title>",
        )

        temp_image_path = _create_staging_path(output_dir, Path(job["dest"]).suffix)
        temp_html_path = (
            _create_staging_path(output_dir, Path(job["final_google_html_path"]).suffix)
            if job["final_google_html_path"] is not None
            else None
        )

        query = scraper.build_query(
            job["artist"],
            job["title"],
            job["format"],
            job["label"],
            job["year"],
            marketplace,
            edition_country=job.get("edition_country", ""),
        )
        log.debug("Query: %r", query)

        image_bytes = scraper.fetch_cover(
            query,
            max_retries=max_retries,
            retry_delay=retry_delay,
            dump_html_path=dump_google_html,
            discogs_hq=discogs_hq,
            browser_fallback=effective_browser_fallback,
            browser_headless=browser_headless,
            captcha_wait_seconds=captcha_wait_timeout,
            reuse_browser_session=effective_reuse_browser_session,
            captcha_cooldown_base_seconds=resolved_cooldown_base,
            captcha_cooldown_max_seconds=resolved_cooldown_max,
            request_label=worker_label,
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

    interrupted = False
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
            completed_rows = 0
            total_rows = len(jobs)
            max_in_flight = max(workers, workers * 4)
            pending_result_updates: dict[int, str] = {}
            pending_update_reported = False

            executor = ThreadPoolExecutor(max_workers=workers)
            jobs_iter = iter(jobs)
            future_map: dict[Any, dict[str, Any]] = {}

            try:
                try:
                    for _ in range(min(max_in_flight, total_rows)):
                        try:
                            next_job = next(jobs_iter)
                        except StopIteration:
                            break
                        future_map[executor.submit(_process_job, next_job)] = next_job

                    while future_map:
                        done, _ = wait(future_map.keys(), return_when=FIRST_COMPLETED)
                        for future in done:
                            job = future_map.pop(future)
                            try:
                                result = future.result()
                            except Exception as exc:
                                log.exception("Worker failed for row %s: %s", job["row_index"], exc)
                                console.print(f"  [red]✗ Worker error:[/] {job['artist']} – {job['title']}")
                                progress.advance(task_id)
                                completed_rows += 1
                                if process_log_every > 0 and completed_rows % process_log_every == 0:
                                    console.print(f"[dim]Processed rows:[/] {completed_rows}/{total_rows}")
                            else:
                                if result["status"] == "saved":
                                    try:
                                        _finalize_staged_result(result)
                                        pending_result_updates[result["row_index"]] = result["filename"]
                                        _record_persisted_pending_update(
                                            csv_path,
                                            persisted_pending_updates,
                                            result["row_index"],
                                            result["filename"],
                                            log,
                                        )
                                        console.print(f"  [green]✓[/] {result['filename']}")
                                    except Exception as exc:
                                        log.exception("Finalize failed for %s: %s", result["filename"], exc)
                                        _cleanup_staged_result(result)
                                        console.print(f"  [red]✗ Finalize error:[/] {result['artist']} – {result['title']}")
                                else:
                                    console.print(f"  [yellow]⚠ No image found for:[/] {result['artist']} – {result['title']}")

                                if len(pending_result_updates) >= _RESULT_UPDATE_BATCH_SIZE:
                                    if _flush_result_updates(csv_path, pending_result_updates, log, persisted_pending_updates):
                                        pending_update_reported = False
                                    elif not pending_update_reported:
                                        console.print(
                                            "[yellow]Spreadsheet update deferred:[/] image files were saved, "
                                            "but the spreadsheet is locked. Close Excel and rerun later to fill rows from disk."
                                        )
                                        pending_update_reported = True

                                progress.advance(task_id)
                                completed_rows += 1
                                if process_log_every > 0 and completed_rows % process_log_every == 0:
                                    console.print(f"[dim]Processed rows:[/] {completed_rows}/{total_rows}")

                            try:
                                next_job = next(jobs_iter)
                            except StopIteration:
                                continue
                            future_map[executor.submit(_process_job, next_job)] = next_job
                except KeyboardInterrupt:
                    interrupted = True
                    console.print("[bold red]Interrupted:[/] stopping workers and cancelling queued jobs...")
                    for future in list(future_map):
                        future.cancel()
                    future_map.clear()

                if pending_result_updates:
                    if _flush_result_updates(csv_path, pending_result_updates, log, persisted_pending_updates):
                        pending_update_reported = False
                    elif not pending_update_reported:
                        console.print(
                            "[yellow]Spreadsheet update deferred:[/] some saved image filenames could not be written to the spreadsheet in this run. "
                            "Rerun the script after closing Excel to auto-fill them from disk."
                        )
            finally:
                executor.shutdown(wait=not interrupted, cancel_futures=interrupted)
    finally:
        scraper.close_browser_session()

    if interrupted:
        removed_staging_files = _cleanup_staging_files(output_dir, log)
        if removed_staging_files > 0:
            console.print(f"[yellow]Removed temporary staging files:[/] {removed_staging_files}")
        os._exit(130)

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


def _resolve_captcha_cooldown_bounds(
    cooldown_range: Optional[str],
    cooldown_base: int,
    cooldown_max: int,
) -> tuple[int, int]:
    """Return cooldown base/max from explicit values or a MIN-MAX range string."""
    if cooldown_range:
        text = cooldown_range.strip()
        parts = text.split("-", 1)
        if len(parts) != 2:
            raise typer.BadParameter(
                "--captcha-cooldown-range must use MIN-MAX format, e.g. 1-50."
            )

        try:
            parsed_min = int(parts[0].strip())
            parsed_max = int(parts[1].strip())
        except ValueError as exc:
            raise typer.BadParameter(
                "--captcha-cooldown-range values must be integers, e.g. 1-50."
            ) from exc

        if parsed_min < 1 or parsed_max < 1:
            raise typer.BadParameter("--captcha-cooldown-range values must be >= 1.")
        if parsed_min > parsed_max:
            raise typer.BadParameter(
                "--captcha-cooldown-range minimum cannot be greater than maximum."
            )
        return parsed_min, parsed_max

    if cooldown_base > cooldown_max:
        raise typer.BadParameter("--captcha-cooldown-base cannot be greater than --captcha-cooldown-max.")

    return cooldown_base, cooldown_max


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


def _cleanup_staging_files(output_dir: Path, log: logging.Logger) -> int:
    """Delete leftover staging temp files from interrupted runs."""
    removed = 0
    for path in output_dir.glob(".stage_*"):
        if not path.is_file():
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except Exception as exc:
            log.warning("Could not remove staging file %s: %s", path, exc)
    return removed


def _pending_updates_path(csv_path: Path) -> Path:
    """Return path for persisted deferred spreadsheet updates."""
    return csv_path.with_name(f"{csv_path.name}{_PENDING_UPDATES_SUFFIX}")


def _load_persisted_pending_updates(csv_path: Path, output_dir: Path, log: logging.Logger) -> dict[int, str]:
    """Load deferred row->filename updates and drop entries whose files are missing."""
    pending_path = _pending_updates_path(csv_path)
    if not pending_path.exists():
        return {}

    try:
        data = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read pending updates file %s: %s", pending_path, exc)
        return {}

    loaded: dict[int, str] = {}
    if isinstance(data, dict):
        for row_index_text, filename in data.items():
            try:
                row_index = int(row_index_text)
            except Exception:
                continue
            image_name = str(filename or "").strip()
            if not image_name:
                continue
            if (output_dir / image_name).is_file():
                loaded[row_index] = image_name

    if loaded:
        log.info("Loaded deferred spreadsheet updates: %d", len(loaded))
    else:
        try:
            pending_path.unlink(missing_ok=True)
        except Exception:
            pass

    return loaded


def _save_persisted_pending_updates(csv_path: Path, updates: dict[int, str], log: logging.Logger) -> None:
    """Persist deferred row->filename updates atomically for future recovery."""
    pending_path = _pending_updates_path(csv_path)
    if not updates:
        try:
            pending_path.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("Could not remove pending updates file %s: %s", pending_path, exc)
        return

    tmp_path = pending_path.with_name(f"{pending_path.name}.tmp")
    payload = {str(k): v for k, v in sorted(updates.items()) if v}
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        os.replace(tmp_path, pending_path)
    except Exception as exc:
        log.warning("Could not persist pending updates to %s: %s", pending_path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _record_persisted_pending_update(
    csv_path: Path,
    persisted_pending_updates: dict[int, str],
    row_index: int,
    filename: str,
    log: logging.Logger,
) -> None:
    """Record one completed row so future runs can avoid re-scraping it."""
    persisted_pending_updates[row_index] = filename
    _save_persisted_pending_updates(csv_path, persisted_pending_updates, log)


def _flush_result_updates(
    csv_path: Path,
    pending_updates: dict[int, str],
    log: logging.Logger,
    persisted_pending_updates: Optional[dict[int, str]] = None,
) -> bool:
    """Persist completed row filenames in a batch; keep them pending on lock errors."""
    if not pending_updates:
        return True

    try:
        csv_handler.update_rows(csv_path, pending_updates)
    except PermissionError as exc:
        log.warning("Spreadsheet update deferred for %d rows: %s", len(pending_updates), exc)
        return False
    except Exception as exc:
        log.exception("Spreadsheet update failed for %d rows: %s", len(pending_updates), exc)
        return False

    if persisted_pending_updates is not None:
        for row_index in pending_updates:
            persisted_pending_updates.pop(row_index, None)
        _save_persisted_pending_updates(csv_path, persisted_pending_updates, log)

    pending_updates.clear()
    return True

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


def _get_row_image_value(row: dict) -> str:
    """Return current image filename value from CSV or XLSX row schemas."""
    return _row_get(row, csv_handler.IMAGE_COLUMN) or _row_get(row, csv_handler.EXCEL_IMAGE_COLUMN)


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


def _extract_country_value(row: dict, country_col: str) -> str:
    """Extract raw country/edition code from configured column or packed payload."""
    direct = _first_segment(_row_get(row, country_col))
    if direct:
        return direct

    payload = _find_semicolon_payload(row)
    if payload:
        columns = _split_semicolon_row(payload)
        # Legacy row shape: Band;Title;;year;FORMAT;Label;RC;PC C;ED;...
        if len(columns) >= 9:
            return _first_segment(columns[8])
    return ""


def _translate_country(raw_country: str, mapping: dict[str, str]) -> str:
    """Translate raw country code using mapping; return empty when unmapped."""
    key = (raw_country or "").strip().upper()
    if not key:
        return ""
    return mapping.get(key, "")


def _first_segment(value: str) -> str:
    """Keep only the first cell-like segment to prevent column leakage."""
    text = (value or "").strip()
    for delim in (";", ","):
        if delim in text:
            text = text.split(delim, 1)[0].strip()
    return text


def _find_existing_image_filename(
    existing_output_names: set[str],
    artist: str,
    title: str,
    format_: str,
) -> str:
    """Return best matching existing filename, preferring names without format suffix."""
    base_no_format = csv_handler.build_image_filename(artist, title, "")
    match = _find_existing_name_for_base(existing_output_names, base_no_format)
    if match:
        return match

    base_with_format = csv_handler.build_image_filename(artist, title, format_)
    match = _find_existing_name_for_base(existing_output_names, base_with_format)
    if match:
        return match

    return ""


def _find_existing_name_for_base(existing_output_names: set[str], base_filename: str) -> str:
    """Find exact or suffixed existing filename for a normalized base filename."""
    if not base_filename or base_filename == ".jpg":
        return ""

    if base_filename in existing_output_names:
        return base_filename

    base_stem = Path(base_filename).stem
    base_suffix = Path(base_filename).suffix or ".jpg"
    prefix = f"{base_stem}_"
    candidates = sorted(
        name
        for name in existing_output_names
        if name.endswith(base_suffix)
        and name.startswith(prefix)
        and name[len(prefix):-len(base_suffix)].isdigit()
    )
    return candidates[0] if candidates else ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
