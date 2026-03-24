# Cover Fetcher

**Cover Fetcher** is a command-line tool that fetches music record cover images
from Google Images and applies a configurable image-processing pipeline to each
downloaded file.

---

## Features

| Feature | Details |
|---|---|
| **CSV-driven** | Reads an existing music-collection CSV and adds an `image_file` column |
| **Resumable** | Re-running the script skips rows whose image already exists on disk |
| **Google Images scraper** | Extracts *inline base64-encoded thumbnails* from Google search responses – no secondary requests to third-party image hosts |
| **Anti-blocking** | Randomised User-Agent rotation, realistic browser headers, per-retry jitter |
| **Marketplace targeting** | Append a custom keyword (e.g. `discogs`, `ebay.com`, `lastfm`) to steer Google towards a specific image source |
| **YAML pipeline** | Configurable post-download transformations: rotation, auto-brightness, auto-saturation |
| **Controlled output** | All images land in a single, configurable download folder |

---

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) (`pip install uv`)
- A system browser (Chrome, Edge, or Firefox) for the optional browser fallback feature

---

## Installation

```bash


# Create virtual environment and install dependencies
uv sync

# Activate the virtual environment (optional – uv run handles this automatically)


```

---

## Quick start

```bash
# Fetch covers for every row in collection.csv
uv run cover-fetcher --csv collection.csv

# Target discogs images
uv run cover-fetcher --csv collection.csv --marketplace discogs

# Custom output folder and pipeline
uv run cover-fetcher \
    --csv collection.csv \
    --marketplace "ebay.com" \
    --output-dir ./covers \
    --pipeline pipeline.yaml
```

---

## CSV format

The input CSV must have (at minimum) these columns (names are configurable via
CLI flags):

| Column | Default flag | Description |
|---|---|---|
| `artist` | `--artist-col` | Artist / band name |
| `title` | `--title-col` | Album / release title |
| `format` | `--format-col` | Release format (LP, CD, 7", …) |
| `label` | `--label-col` | Record label |
| `year` | `--year-col` | Release year |

After processing, an `image_file` column is added / updated with the
downloaded image filename.

---

## Image naming

Downloaded images follow this pattern:

```
{artist}_{title}_{format}_{YYYY-MM-DD}.jpg
```

All characters unsafe for filenames (`/ : * ? " < > |`) are replaced with
underscores.

---

## Pipeline configuration (`pipeline.yaml`)

```yaml
pipeline:
  - step: auto_brightness
    clip_percent: 1.0      # % of pixels clipped at each histogram end

  - step: auto_saturation
    clip_percent: 1.0

  # Optional rotation (clockwise)
  # - step: rotate
  #   degrees: 90
  #   expand: true
```

Place `pipeline.yaml` in the working directory (auto-detected) or pass an
explicit path via `--pipeline`.

---

## All CLI options

```
cover-fetcher --help
```

| Option | Default | Description |
|---|---|---|
| `--csv` | *(required)* | Path to the CSV file |
| `--marketplace` | `""` | Extra keyword added to every Google query |
| `--output-dir` | `downloads/` | Folder for downloaded images |
| `--pipeline` | auto-detect | Path to the YAML pipeline config |
| `--artist-col` | `artist` | CSV column for artist |
| `--title-col` | `title` | CSV column for title |
| `--format-col` | `format` | CSV column for format |
| `--label-col` | `label` | CSV column for label |
| `--year-col` | `year` | CSV column for year |
| `--max-retries` | `3` | Fetch retries per row |
| `--retry-delay` | `2.0` | Base delay (s) between retries |
| `--dump-google-html` | (none) | Write raw Google HTML responses to disk for debugging |
| `--browser-fallback` | on | Use a real browser when Google challenges the HTTP scraper |
| `--browser-headless` | on | Run the browser fallback headlessly |
| `--captcha-wait-timeout` | `600` | Seconds to wait for manual CAPTCHA solving before giving up (10 min) |
| `--verbose` / `-v` | off | Enable debug logging |

---

## Running tests

```bash
uv run pytest
```

---

## Project structure

```
musee/
├── pyproject.toml          # project & uv configuration
├── pipeline.yaml           # default image processing pipeline
├── downloads/              # default image output folder
├── src/
│   └── cover_fetcher/
│       ├── __init__.py
│       ├── cli.py          # Typer CLI entry point
│       ├── scraper.py      # Google Images base64 thumbnail scraper
│       ├── processor.py    # YAML-driven image pipeline
│       └── csv_handler.py  # CSV read / write / resume logic
└── tests/
    ├── test_scraper.py
    ├── test_csv_handler.py
    └── test_processor.py
```
