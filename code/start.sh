#!/bin/bash
#keeps html --keep-google-html-only
#with no pipeline specification
#uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir "/g/oscar/records/BD/covers" --pipeline ./pipelines/default.yaml --browser-visible --workers 8 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq
uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir "/g/oscar/records/BD/covers" --pipeline-col "seller" --browser-visible --workers 8 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq
