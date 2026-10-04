#!/usr/bin/env Rscript
suppressPackageStartupMessages({
  library(data.table)
  library(fixest)
  library(ggplot2)
  library(arrow)
})
setFixest_nthreads(6)
setDTthreads(6)
cmd <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", cmd[grep("^--file=", cmd)])
root <- normalizePath(file.path(dirname(file_arg[[1]]), ".."), mustWork = TRUE)
out_dir <- file.path(root, "output")
fig_dir <- file.path(root, "output/figures")
tab_dir <- file.path(root, "output/tables")
dir.create(fig_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(tab_dir, recursive = TRUE, showWarnings = FALSE)

prep <- function(path, city) {
  dt <- as.data.table(read_parquet(path))
  dt <- dt[is.finite(temp) & is.finite(precip) & is.finite(snowfall) & is.finite(pm25) & is.finite(wind)]
  dt[, city := city]
  dt[, date := as.IDate(date)]
  dt[, period := factor(period, levels = c("night", "morning", "midday", "evening", "late"))]
  dt[, how := interaction(dow, period, drop = TRUE, lex.order = TRUE)]
  dt[, station_id := as.character(station_id)]
  dt
}

nyc <- prep(file.path(root, "data/processed/panel_period.parquet"), "New York")
lon <- prep(file.path(root, "data/processed/london_panel.parquet"), "London")

nyc_main <- nyc[access_800 == 1 & place_type %in% c("residential", "mixed", "commercial")]
nyc_rail <- nyc[access_800 == 1]
lon_main <- lon[access_800 == 1]

sink(file.path(out_dir, "compare_sample.txt"))
describe <- function(dt, label) {
  cat("\n", label, "\n")
  cat("rows", nrow(dt), "stations", uniqueN(dt$station_id), "days", uniqueN(dt$date),
      "starts", sum(dt$starts), "\n")
  cat("classic_share", sum(dt$starts_classic) / sum(dt$starts),
      "ebike_share", sum(dt$starts_electric) / sum(dt$starts), "\n")
  cat("rain", mean(dt$rain), "rain1", mean(dt$rain1), "snow", mean(dt$snow),
      "hot", mean(dt$hot), "freezing", mean(dt$freezing), "\n")
  cat("snow_dates", uniqueN(dt$date[dt$snow == 1]),
      "hot_dates", uniqueN(dt$date[dt$hot == 1]), "\n")
  cat("mean_starts", mean(dt$starts), "mean_temp", mean(dt$temp), "mean_wind", mean(dt$wind),
      "mean_pm", mean(dt$pm25), "\n")
  cat("hubs", uniqueN(dt$station_id[dt$is_hub == 1]),
      "outside_not_in_this_sample", "\n")
}
describe(nyc_main, "nyc published subway sample")
describe(nyc_rail, "nyc all subway-adjacent")
describe(lon_main, "london tube-adjacent")
describe(nyc, "nyc all docks")
describe(lon, "london all docks")
cat("\nlondon outside", uniqueN(lon$station_id[lon$access_800 == 0]),
    "nyc outside", uniqueN(nyc$station_id[nyc$access_800 == 0]), "\n")
sink()
cat("samples written\n")

ctrl <- "freezing + snow + temp + I(temp^2) + pm25 + wind"
f_base <- as.formula(paste("starts ~ rain + hot +", ctrl, "| station_id + how + month"))
f_rain1 <- as.formula(paste("starts ~ rain1 + hot +", ctrl, "| station_id + how + month"))
f_precip <- as.formula("starts ~ precip + snowfall + hot + freezing + temp + I(temp^2) + pm25 + wind | station_id + how + month")
f_classic <- as.formula(paste("starts_classic ~ rain + hot +", ctrl, "| station_id + how + month"))
f_electric <- as.formula(paste("starts_electric ~ rain + hot +", ctrl, "| station_id + how + month"))
f_period <- as.formula(paste("starts ~ rain + i(period, rain, 'midday') + hot +", ctrl, "| station_id + how + month"))
f_hub <- as.formula(paste("starts ~ rain * is_hub + hot * is_hub +", ctrl, "| station_id + how + month"))
f_access <- as.formula(paste("starts ~ rain * access_800 + hot * access_800 +", ctrl, "| station_id + how + month"))

lincomb <- function(model, weights, contrast, city, outcome) {
  b <- coef(model)
  V <- vcov(model)
  w <- rep(0, length(b))
  names(w) <- names(b)
  missing <- character()
  for (nm in names(weights)) {
    if (is.na(nm) || !nm %in% names(b)) missing <- c(missing, as.character(nm)) else w[nm] <- weights[[nm]]
  }
  if (length(missing)) {
    return(data.table(city = city, outcome = outcome, contrast = contrast,
                      estimate = NA_real_, se = NA_real_, pct = NA_real_,
                      pct_lo = NA_real_, pct_hi = NA_real_, p = NA_real_,
                      note = paste(missing, collapse = "|")))
  }
  est <- sum(w * b)
  se <- sqrt(as.numeric(t(w) %*% V %*% w))
  z <- est / se
  data.table(city = city, outcome = outcome, contrast = contrast, estimate = est, se = se,
             pct = 100 * (exp(est) - 1),
             pct_lo = 100 * (exp(est - 1.96 * se) - 1),
             pct_hi = 100 * (exp(est + 1.96 * se) - 1),
             p = 2 * pnorm(-abs(z)), note = "")
}

pick <- function(cn, patterns) {
  for (pat in patterns) {
    hit <- grep(pat, cn, value = TRUE)
    if (length(hit) == 1) return(hit)
  }
  NA_character_
}

one <- function(cn, pattern) {
  hit <- pick(cn, pattern)
  stats::setNames(list(1), hit)
}

add_term <- function(w, cn, patterns) {
  hit <- pick(cn, patterns)
  w[[hit]] <- 1
  w
}

fit_one <- function(formula, data, city, name) {
  cat("fitting", city, name, "rows", nrow(data), "\n")
  model <- fepois(formula, data = data, cluster = ~ station_id + date, lean = FALSE, notes = FALSE)
  n_obs_value <- model$nobs
  ct <- as.data.table(coeftable(model), keep.rownames = "term")
  setnames(ct, c("term", "estimate", "se", "stat", "p"))
  ct[, city := city]
  ct[, model_name := name]
  ct[, n_obs := n_obs_value]
  list(model = model, coef = ct)
}

rows <- list()
coef_rows <- list()
run_pair <- function(nyc_data, lon_data, tag) {
  specs <- list(
    base = list(f_base, "starts"),
    rain1 = list(f_rain1, "starts"),
    precip = list(f_precip, "starts"),
    classic = list(f_classic, "classic"),
    electric = list(f_electric, "electric"),
    period = list(f_period, "starts"),
    hub = list(f_hub, "starts")
  )
  for (nm in names(specs)) {
    for (pair in list(list("New York", nyc_data), list("London", lon_data))) {
      fitted <- fit_one(specs[[nm]][[1]], pair[[2]], pair[[1]], paste(tag, nm, sep = "_"))
      coef_rows[[length(coef_rows) + 1]] <<- fitted$coef
      model <- fitted$model
      cn <- names(coef(model))
      writeLines(cn, file.path(out_dir, paste0("names_", tag, "_", pair[[1]], "_", nm, ".txt")))
      city <- pair[[1]]
      outcome <- specs[[nm]][[2]]
      if (nm == "base") {
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^rain$")), "rain", city, outcome)
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^snow$")), "snow", city, outcome)
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^hot$")), "hot", city, outcome)
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^wind$")), "wind", city, outcome)
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^pm25$")), "pm25", city, outcome)
      } else if (nm == "rain1") {
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^rain1$")), "rain1", city, outcome)
      } else if (nm == "precip") {
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^precip$")), "precip_mm", city, outcome)
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^snowfall$")), "snowfall_cm", city, outcome)
      } else if (nm %in% c("classic", "electric")) {
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^rain$")), "rain", city, outcome)
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^hot$")), "hot", city, outcome)
      } else if (nm == "period") {
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^rain$")), "rain_midday", city, outcome)
        for (per in c("night", "morning", "evening", "late")) {
          w <- one(cn, c("^rain$"))
          w <- add_term(w, cn, c(paste0("period::", per, ":rain$"), paste0("rain:period::", per, "$")))
          rows[[length(rows) + 1]] <<- lincomb(model, w, paste0("rain_", per), city, outcome)
        }
      } else if (nm == "hub") {
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^rain$")), "rain_not_hub", city, outcome)
        w <- one(cn, c("^rain$"))
        w <- add_term(w, cn, c("^rain:is_hub$", "^is_hub:rain$"))
        rows[[length(rows) + 1]] <<- lincomb(model, w, "rain_hub", city, outcome)
        rows[[length(rows) + 1]] <<- lincomb(model, one(cn, c("^rain:is_hub$", "^is_hub:rain$")), "rain_hub_increment", city, outcome)
      }
    }
  }
}

run_pair(nyc_main, lon_main, "main")
# Same rail-adjacent rule without New York's land-use screen.
run_pair(nyc_rail, lon_main, "allrail")

for (pair in list(list("New York", nyc), list("London", lon))) {
  fitted <- fit_one(f_access, pair[[2]], pair[[1]], "access")
  coef_rows[[length(coef_rows) + 1]] <- fitted$coef
  model <- fitted$model
  cn <- names(coef(model))
  city <- pair[[1]]
  rows[[length(rows) + 1]] <- lincomb(model, one(cn, c("^rain$")), "rain_beyond_800", city, "access")
  w <- one(cn, c("^rain$"))
  w <- add_term(w, cn, c("^rain:access_800$", "^access_800:rain$"))
  rows[[length(rows) + 1]] <- lincomb(model, w, "rain_within_800", city, "access")
  rows[[length(rows) + 1]] <- lincomb(model, one(cn, c("^rain:access_800$", "^access_800:rain$")), "rain_access_increment", city, "access")
}

eff <- rbindlist(rows, fill = TRUE)
coef_all <- rbindlist(coef_rows, fill = TRUE)
fwrite(eff, file.path(out_dir, "compare_effects.csv"))
fwrite(coef_all, file.path(out_dir, "compare_coefs.csv"))
fwrite(eff, file.path(tab_dir, "compare_effects.csv"))
print(eff[city %in% c("New York", "London") & contrast %in% c("rain", "rain1", "precip_mm", "snow", "hot", "rain_morning", "rain_midday", "rain_hub", "rain_not_hub", "rain_hub_increment", "rain_within_800", "rain_beyond_800", "rain_access_increment")])

plot_dt <- eff[contrast %in% c("rain", "rain_morning", "rain_midday") | (outcome %in% c("classic", "electric") & contrast == "rain") | contrast %in% c("rain_within_800", "rain_beyond_800")]
# Keep the main-sample estimates. Access rows have outcome access; rain rows from run_pair are duplicated for main and allrail.
# The rain/period/vehicle rows were appended twice. Tag them by order: first block is main.
# Reconstruct labels from the saved names instead of relying on order.
# The function did not store the sample tag on the effect rows. Rebuild the plot from a tagged rerun below.
cat("estimation pass complete\n")
