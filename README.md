# Metro-adjacent bike-share weather

Replication files for the study of rain, land use and bicycle type at metro-adjacent bike-share stations in New York and London. The repository contains the processing scripts, the station-period panels and the coefficient tables. It does not contain the raw trip archives.

## What is included

- `data/processed/panel_period.parquet`: New York station-period panel for 2025.
- `data/processed/london_panel.parquet`: London station-period panel for 2025.
- `data/processed/stations.csv` and `data/processed/london_stations.csv`: dock coordinates, metro distance and place attributes.
- `output/`: Poisson coefficient tables, percentage effects and figures.
- `code/`: panel construction and estimation.

The two cities are estimated separately. They are not pooled in one regression.

## Reproduce the estimates

The published numbers come from the processed panels. From the repository root:

```bash
Rscript code/estimate.R
Rscript code/estimate_compare.R
Rscript code/fleet_gap.R
Rscript code/plot_compare.R
Rscript code/export_figures_600.R
```

R packages: `data.table`, `fixest`, `ggplot2`, `arrow`.

The estimator is Poisson pseudo-maximum likelihood in `fixest`. Fixed effects are the station, the period of the week and the month. Standard errors are clustered by station and date. A coefficient `b` is reported as `100 * (exp(b) - 1)`, with a delta-method interval from the clustered standard error. The e-bike gap is a stacked Poisson contrast of the two fleets, not a comparison of overlapping intervals.

Figures are written to `output/figures/`. Coefficient tables from a fresh run are written to `output/` and `output/tables/`.

## Rebuild the panels

Raw trip files are redistributed by the operators and are not in this repository.

New York:

1. `bash code/download_citibike_2025.sh` saves the twelve monthly Citi Bike archives in `data/raw/citibike/`. The public index is [https://s3.amazonaws.com/tripdata/index.html](https://s3.amazonaws.com/tripdata/index.html).
2. `python code/download_pluto.py` writes MapPLUTO lot records to `data/raw/gis/pluto_lots.csv`.
3. Unzip the MTA subway GTFS feed into `data/interim/gtfs/` so that `stops.txt`, `stop_times.txt`, `trips.txt` and `routes.txt` are in that folder. The feed is published by the MTA.
4. Place the Open-Meteo weather and air-quality archives used by the panel at `data/raw/weather/nyc_weather_utc_2025.json` and `data/raw/weather/nyc_aq_utc_2025.json`.
5. `python code/aggregate_citibike.py`, then `python code/build_panel.py`.

London:

`python code/london_pipeline.py` reads the 2025 Santander Cycles journey files from the TfL cycling-data bucket, the TfL BikePoint and Underground feeds, and the Open-Meteo archive and air-quality APIs. Journey files are saved under `data/raw/london/usage/`.

Python packages: `duckdb`, `requests`.

## Sample rules

The New York estimation sample keeps docks within 800 metres of a subway parent station whose surrounding lots are residential, mixed or commercial. The London estimation sample keeps docks within 800 metres of an Underground station with at least 200 starts in 2025. The public London journey extract has no membership field and no lot-level land use, so those two contrasts are estimated for New York only.
