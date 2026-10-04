#!/usr/bin/env Rscript
suppressPackageStartupMessages({library(data.table); library(fixest); library(arrow)})
setFixest_nthreads(6)
cmd <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", cmd[grep("^--file=", cmd)])
root <- normalizePath(file.path(dirname(file_arg[[1]]), ".."), mustWork = TRUE)
prep <- function(path, city, screen) {
  dt <- as.data.table(read_parquet(path))
  dt <- dt[is.finite(temp) & is.finite(precip) & is.finite(snowfall) & is.finite(pm25) & is.finite(wind)]
  dt <- dt[access_800 == 1]
  if (screen) dt <- dt[place_type %in% c("residential", "mixed", "commercial")]
  dt[, date := as.IDate(date)]
  dt[, period := factor(period, levels = c("night","morning","midday","evening","late"))]
  dt[, how := interaction(dow, period, drop = TRUE, lex.order = TRUE)]
  dt[, station_id := as.character(station_id)]
  long <- rbind(
    dt[, .(station_id, date, how, month, starts = starts_classic, ebike = 0, rain, hot, freezing, snow, temp, pm25, wind)],
    dt[, .(station_id, date, how, month, starts = starts_electric, ebike = 1, rain, hot, freezing, snow, temp, pm25, wind)]
  )
  long[, ebike := factor(ebike)]
  cat("fit", city, "rows", nrow(long), "\n")
  m <- fepois(starts ~ rain * ebike + hot * ebike + freezing + snow + temp + I(temp^2) + pm25 + wind |
                station_id^ebike + how^ebike + month^ebike,
              data = long, cluster = ~ station_id + date, lean = TRUE, notes = FALSE)
  ct <- coeftable(m)
  print(ct[grep("rain", rownames(ct)), , drop = FALSE])
  b <- coef(m); V <- vcov(m)
  # total ebike rain = rain + rain:ebike1
  nm <- grep("^rain:ebike", names(b), value = TRUE)
  cat("interaction name", nm, "\n")
  w <- rep(0, length(b)); names(w) <- names(b); w["rain"] <- 1; w[nm] <- 1
  est <- sum(w * b); se <- sqrt(as.numeric(t(w) %*% V %*% w))
  cat(city, "classic pct", 100*(exp(b["rain"])-1), "\n")
  cat(city, "ebike pct", 100*(exp(est)-1), "se_log", se, "\n")
  cat(city, "gap log", b[nm], "se", sqrt(V[nm, nm]), "p", 2*pnorm(-abs(b[nm]/sqrt(V[nm,nm]))), "\n")
}
nyc <- prep(file.path(root, "data/processed/panel_period.parquet"), "New York", TRUE)
lon <- prep(file.path(root, "data/processed/london_panel.parquet"), "London", FALSE)
