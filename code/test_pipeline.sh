#!/bin/bash
uv run cover-fetcher --csv "records.xlsx" --marketplace "discogs" --output-dir "./covers"  --pipeline-col "seller"  --browser-visible --workers 1 --process-log-every 100 --max-jobs 1500 --captcha-cooldown-range 3-10  --discogs-hq
cd covers
start .
