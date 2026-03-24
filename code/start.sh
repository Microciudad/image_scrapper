#!/bin/bash
#keeps html --keep-google-html-only
uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir ./covers --pipeline pipeline.yaml --browser-visible --workers 4 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq