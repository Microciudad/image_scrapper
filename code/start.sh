#!/bin/bash
uv run cover-fetcher --csv records.csv --marketplace "discogs" --output-dir ./covers --pipeline pipeline.yaml --keep-google-html-only --browser-visible --workers 8