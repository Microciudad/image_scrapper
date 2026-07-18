#!/bin/bash
#keeps html --keep-google-html-only
#with no pipeline specification
#uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir "/g/oscar/records/BD/covers" --pipeline ./pipelines/default.yaml --browser-visible --workers 8 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq


#main
#test2
#uv run cover-fetcher --csv "records.xlsx"  --marketplace "discogs" --output-dir "./covers" --pipeline-col "seller" --browser-visible --workers 8 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq --max-filename-length 100
#
#uv run cover-fetcher --csv "records.xlsx"  --marketplace "discogs" --output-dir "./covers" --pipeline-col "seller" --browser-visible --workers 8 --process-log-every 1000 --max-jobs 15000 --captcha-cooldown-range 3-10  --discogs-hq   --do-not-fetch-and-use ./source.jpg
#uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir "/g/oscar/records/BD/covers" --pipeline-col "seller" --browser-visible --workers 8 --process-log-every 1000 --max-jobs 20000 --captcha-cooldown-range 3-10  --discogs-hq --max-filename-length 100

echo "Cierra LISTA_VEN!!! Presiona Enter para continuar..."
read a

cd "/g/oscar/develop/image_scrapper/code"
#uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir "/g/oscar/records/BD/covers" --pipeline-col "seller" --browser-visible --workers 8 --process-log-every 1000 --max-jobs 20000 --captcha-cooldown-range 3-10  --discogs-hq --max-filename-length 100 --image-index 2 --watch-and-refill --overwrite-existing-on-disk
# Optimized for performance: 40 workers with 100-row batching
uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --marketplace "discogs" --output-dir "/g/oscar/records/BD/covers" --pipeline-col "seller" --browser-visible --workers 40 --process-log-every 1000 --max-jobs 30000 --captcha-cooldown-range 3-10  --discogs-hq --max-filename-length 100 --image-index 2 --watch-and-refill --request-delay-range 0.5-1.5


#cleanup images not in spreadsheet 
#uv run cover-fetcher --csv "/g/oscar/records/BD/LISTA_VEN.xlsx" --images-dir "/g/oscar/records/BD/covers" --cleanup-orphan-images

echo "Press Enter to exit"
read a


