#!/bin/bash
source .venv/Scripts/activate
uv run cover-fetcher --input-image ./image_sample.jpg --output-image ./image_sample_output.jpg --pipeline ./pipeline.yaml
start ./image_sample_output.jpg