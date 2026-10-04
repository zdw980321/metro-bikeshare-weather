#!/bin/bash
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
dest="$root/data/raw/citibike"
mkdir -p "$dest"
cd "$dest"
for m in 01 02 03 04 05 06 07 08 09 10 11 12; do
  f="2025${m}-citibike-tripdata.zip"
  url="https://s3.amazonaws.com/tripdata/${f}"
  echo "START $f"
  curl -L --fail --retry 5 --retry-delay 2 -C - -o "$f" "$url"
  echo "DONE $f"
done
