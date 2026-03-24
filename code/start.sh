#!/bin/bash
#keeps html --keep-google-html-only
uv run cover-fetcher --csv records.xlsx --marketplace "discogs" --output-dir ./covers --pipeline pipeline.yaml --browser-visible --workers 1 --process-log-every 1000 --max-jobs 500 --captcha-cooldown-range 3-10