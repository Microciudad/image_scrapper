#!/bin/bash
#keeps html --keep-google-html-only
#with no pipeline specification
#uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir "/g/oscar/records/BD/covers" --pipeline ./pipelines/default.yaml --browser-visible --workers 8 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq


#main
#test
#uv run cover-fetcher --csv "records.xlsx"  --marketplace "discogs" --output-dir "./covers" --pipeline-col "seller" --browser-visible --workers 8 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq --max-filename-length 100
#
#uv run cover-fetcher --csv "records.xlsx"  --marketplace "discogs" --output-dir "./covers" --pipeline-col "seller" --browser-visible --workers 8 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq   --do-not-fetch-and-use ./source.jpg
uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir "/g/oscar/records/BD/covers" --pipeline-col "seller" --browser-visible --workers 8 --process-log-every 1000 --max-jobs 20000 --captcha-cooldown-range 3-10  --discogs-hq --max-filename-length 100


#cleanup images not in spreadsheet 
#uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --images-dir "/g/oscar/records/BD/covers" --cleanup-orphan-images





