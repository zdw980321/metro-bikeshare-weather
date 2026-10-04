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
panel_path <- file.path(root, "data/processed/panel_period.parquet")
fig_dir <- file.path(root, "output/figures")
tab_dir <- file.path(root, "output/tables")
out_dir <- file.path(root, "output")
dir.create(fig_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(tab_dir, recursive = TRUE, showWarnings = FALSE)
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

cols <- c("station_id", "date", "period", "place_type", "is_hub", "access_300", "access_800",
          "starts", "starts_member", "starts_casual", "starts_classic", "starts_electric", "ends",
          "temp", "precip", "snowfall", "wind", "pm25", "rain", "rain1", "snow", "hot", "hot30",
          "freezing", "high_pm", "dow", "month")
dt <- as.data.table(read_parquet(panel_path, col_select = all_of(cols)))
dt <- dt[is.finite(temp) & is.finite(precip) & is.finite(snowfall) & is.finite(pm25) & is.finite(wind)]
dt[, date := as.IDate(date)]
dt[, period := factor(period, levels = c("night", "morning", "midday", "evening", "late"))]
dt[, how := interaction(dow, period, drop = TRUE, lex.order = TRUE)]
dt[, station_id := as.character(station_id)]
dt[, net_flow := ends - starts]
dt[, place_type := factor(place_type, levels = c("residential", "mixed", "commercial", "other", "unclassified"))]

sink(file.path(out_dir, "sample_summary.txt"))
cat("rows", nrow(dt), "stations", uniqueN(dt$station_id), "days", uniqueN(dt$date), "\n")
print(dt[, .(rows = .N, stations = uniqueN(station_id), starts = sum(starts),
             mean_starts = mean(starts), rain = mean(rain), rain1 = mean(rain1),
             snow = mean(snow), hot = mean(hot), freezing = mean(freezing),
             high_pm = mean(high_pm), pm = mean(pm25), temp = mean(temp)),
          by = .(access_800, place_type)])
print(dt[access_800 == 1, .(stations = uniqueN(station_id), hub_stations = uniqueN(station_id[is_hub == 1])), by = place_type])
sink()
cat("sample written\n")

base <- dt[access_800 == 1 & place_type %in% c("residential", "mixed", "commercial")]
base[, place_type := factor(place_type, levels = c("residential", "mixed", "commercial"))]
cat("base rows", nrow(base), "stations", uniqueN(base$station_id), "\n")

fe <- stats::as.formula("starts ~ . | station_id + how + month")
# formulas are written explicitly below
ctrl <- "freezing + snow + temp + I(temp^2) + pm25 + wind"
f_base <- as.formula(paste("starts ~ rain + hot +", ctrl, "| station_id + how + month"))
f_rain1 <- as.formula(paste("starts ~ rain1 + hot +", ctrl, "| station_id + how + month"))
f_precip <- as.formula(paste("starts ~ precip + snowfall + hot + freezing + temp + I(temp^2) + pm25 + wind | station_id + how + month"))
f_het <- as.formula(paste(
  "starts ~ rain + i(place_type, rain, 'residential') + rain:is_hub",
  "+ hot + i(place_type, hot, 'residential') + hot:is_hub",
  "+ i(place_type, pm25, 'residential') +", ctrl,
  "| station_id + how + month"))
f_user <- function(y) as.formula(paste(
  y, "~ rain + i(place_type, rain, 'residential') + rain:is_hub",
  "+ hot + i(place_type, hot, 'residential') + hot:is_hub +", ctrl,
  "| station_id + how + month"))
f_access <- as.formula(paste(
  "starts ~ rain + rain:access_800 + hot + hot:access_800 +", ctrl,
  "| station_id + how + month"))
f_period <- as.formula(paste(
  "starts ~ rain + i(period, rain, 'midday') + hot +", ctrl,
  "| station_id + how + month"))

export_coef <- function(model, name) {
  n_obs_value <- model$nobs
  wr <- tryCatch(as.numeric(r2(model, "wr2")), error = function(e) NA_real_)
  ct <- as.data.table(coeftable(model), keep.rownames = "term")
  setnames(ct, c("term", "estimate", "se", "stat", "p"))
  ct[, model_name := name]
  ct[, n_obs := n_obs_value]
  ct[, within_r2 := wr[1]]
  fwrite(ct, file.path(out_dir, paste0("coef_", name, ".csv")))
  writeLines(names(coef(model)), file.path(out_dir, paste0("names_", name, ".txt")))
  ct
}

lincomb <- function(model, weights, contrast, outcome) {
  b <- coef(model)
  V <- vcov(model)
  w <- rep(0, length(b))
  names(w) <- names(b)
  missing <- character()
  for (nm in names(weights)) {
    if (!nm %in% names(b)) missing <- c(missing, nm) else w[nm] <- weights[[nm]]
  }
  if (length(missing)) {
    return(data.table(outcome = outcome, contrast = contrast, estimate = NA_real_, se = NA_real_,
                      pct = NA_real_, pct_lo = NA_real_, pct_hi = NA_real_, p = NA_real_,
                      note = paste(missing, collapse = "|")))
  }
  est <- sum(w * b)
  se <- sqrt(as.numeric(t(w) %*% V %*% w))
  z <- est / se
  data.table(outcome = outcome, contrast = contrast, estimate = est, se = se,
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

rain_weights <- function(cn, place = "residential", hub = 0) {
  w <- list()
  main <- pick(cn, c("^rain$"))
  w[[main]] <- 1
  if (place != "residential") {
    hit <- pick(cn, c(
      paste0("place_type::", place, ":rain$"),
      paste0("^rain:place_type::", place, "$"),
      paste0(place, ":rain$")
    ))
    w[[hit]] <- 1
  }
  if (hub == 1) {
    hit <- pick(cn, c("^rain:is_hub$", "^is_hub:rain$", "rain:is_hub"))
    w[[hit]] <- 1
  }
  w
}

hot_weights <- function(cn, place = "residential", hub = 0) {
  w <- list()
  w[[pick(cn, c("^hot$"))]] <- 1
  if (place != "residential") {
    hit <- pick(cn, c(
      paste0("place_type::", place, ":hot$"),
      paste0("^hot:place_type::", place, "$"),
      paste0(place, ":hot$")
    ))
    w[[hit]] <- 1
  }
  if (hub == 1) {
    hit <- pick(cn, c("^hot:is_hub$", "^is_hub:hot$", "hot:is_hub"))
    w[[hit]] <- 1
  }
  w
}

pm_weights <- function(cn, place = "residential") {
  w <- list()
  w[[pick(cn, c("^pm25$"))]] <- 1
  if (place != "residential") {
    hit <- pick(cn, c(
      paste0("place_type::", place, ":pm25$"),
      paste0("^pm25:place_type::", place, "$"),
      paste0(place, ":pm25$")
    ))
    w[[hit]] <- 1
  }
  w
}

effects_for <- function(model, outcome) {
  cn <- names(coef(model))
  rows <- list()
  for (place in c("residential", "mixed", "commercial")) {
    rows[[length(rows) + 1]] <- lincomb(model, rain_weights(cn, place, 0), paste0("rain_", place), outcome)
    rows[[length(rows) + 1]] <- lincomb(model, hot_weights(cn, place, 0), paste0("hot_", place), outcome)
    if (any(grepl("pm25", cn))) {
      rows[[length(rows) + 1]] <- lincomb(model, pm_weights(cn, place), paste0("pm25_", place), outcome)
    }
  }
  rows[[length(rows) + 1]] <- lincomb(model, rain_weights(cn, "residential", 1), "rain_residential_hub", outcome)
  rbindlist(rows, fill = TRUE)
}

run_pois <- function(formula, data, name) {
  cat("fitting", name, "rows", nrow(data), "\n")
  model <- fepois(formula, data = data, cluster = ~ station_id + date, lean = FALSE, notes = FALSE)
  export_coef(model, name)
  model
}

m_base <- run_pois(f_base, base, "base")
m_rain1 <- run_pois(f_rain1, base, "rain1")
m_precip <- run_pois(f_precip, base, "precip")
m_het <- run_pois(f_het, base, "het")
m_member <- run_pois(f_user("starts_member"), base, "member")
m_casual <- run_pois(f_user("starts_casual"), base, "casual")
m_classic <- run_pois(f_user("starts_classic"), base, "classic")
m_electric <- run_pois(f_user("starts_electric"), base, "electric")
m_period <- run_pois(f_period, base, "period")
m_300 <- run_pois(f_het, base[access_300 == 1], "het300")
m_access <- run_pois(f_access, dt, "access")
m_access_res <- run_pois(f_access, dt[place_type == "residential"], "access_res")
m_net <- feols(as.formula(paste(
  "net_flow ~ rain + i(place_type, rain, 'residential') + rain:is_hub + hot +", ctrl,
  "| station_id + how + month")), data = base, cluster = ~ station_id + date, notes = FALSE)
export_coef(m_net, "net")

eff <- rbindlist(list(
  effects_for(m_base, "starts"),
  effects_for(m_het, "starts_het"),
  effects_for(m_member, "member"),
  effects_for(m_casual, "casual"),
  effects_for(m_classic, "classic"),
  effects_for(m_electric, "electric"),
  effects_for(m_300, "starts_300")
), fill = TRUE)
# access total effects
cn <- names(coef(m_access))
acc_rows <- list(
  lincomb(m_access, setNames(list(1), pick(cn, c("^rain$"))), "rain_not_subway", "access"),
  lincomb(m_access, {
    w <- list(); w[[pick(cn, c("^rain$"))]] <- 1
    hit <- pick(cn, c("^rain:access_800$", "^access_800:rain$", "rain:access_800"))
    w[[hit]] <- 1; w
  }, "rain_subway", "access")
)
# period total effects
cn <- names(coef(m_period))
cnr <- names(coef(m_access_res))
acc_res_rows <- list(
  lincomb(m_access_res, setNames(list(1), pick(cnr, c("^rain$"))), "rain_not_subway", "access_res"),
  lincomb(m_access_res, {
    w <- list(); w[[pick(cnr, c("^rain$"))]] <- 1
    hit <- pick(cnr, c("^rain:access_800$", "^access_800:rain$", "rain:access_800"))
    w[[hit]] <- 1; w
  }, "rain_subway", "access_res")
)
per_rows <- list(lincomb(m_period, setNames(list(1), pick(cn, c("^rain$"))), "rain_midday", "period"))
for (per in c("night", "morning", "evening", "late")) {
  w <- list(); w[[pick(cn, c("^rain$"))]] <- 1
  hit <- pick(cn, c(paste0("period::", per, ":rain$"), paste0("^rain:period::", per, "$"), paste0(per, ":rain$")))
  w[[hit]] <- 1
  per_rows[[length(per_rows) + 1]] <- lincomb(m_period, w, paste0("rain_", per), "period")
}
eff <- rbindlist(list(eff, rbindlist(acc_rows, fill = TRUE), rbindlist(acc_res_rows, fill = TRUE), rbindlist(per_rows, fill = TRUE)), fill = TRUE)
fwrite(eff, file.path(tab_dir, "percent_effects.csv"))
fwrite(eff, file.path(out_dir, "percent_effects.csv"))
print(eff)

# descriptive figure
plot_dt <- base[, .(starts = mean(starts)), by = .(place_type, period, rain)]
p <- ggplot(plot_dt, aes(period, starts, fill = factor(rain))) +
  geom_col(position = "dodge", width = 0.7) +
  facet_wrap(~place_type) +
  scale_fill_manual(values = c("0" = "#0072B2", "1" = "#E69F00"), labels = c("No rain", "Rain")) +
  labs(x = NULL, y = "Mean trip starts per station-period", fill = NULL) +
  theme_minimal(base_size = 12) +
  theme(axis.text.x = element_text(angle = 30, hjust = 1))
ggsave(file.path(fig_dir, "starts_by_rain_place.png"), p, width = 9, height = 5.2, dpi = 220)
ggsave(file.path(out_dir, "starts_by_rain_place.png"), p, width = 9, height = 5.2, dpi = 220)
fwrite(plot_dt, file.path(out_dir, "starts_by_rain_place.csv"))

rain_plot <- eff[outcome == "starts_het" & grepl("^rain_", contrast) & contrast != "rain_residential_hub"]
rain_plot[, place := sub("^rain_", "", contrast)]
if (nrow(rain_plot) && all(is.finite(rain_plot$pct))) {
  p2 <- ggplot(rain_plot, aes(pct, place)) +
    geom_vline(xintercept = 0, linetype = 2) +
    geom_errorbarh(aes(xmin = pct_lo, xmax = pct_hi), height = 0.15) +
    geom_point(size = 2.6) +
    labs(x = "Change in trip starts during rain (%)", y = NULL) +
    theme_minimal(base_size = 12)
  ggsave(file.path(fig_dir, "rain_effects.png"), p2, width = 7.2, height = 3.6, dpi = 220)
  ggsave(file.path(out_dir, "rain_effects.png"), p2, width = 7.2, height = 3.6, dpi = 220)
}

veh <- eff[outcome %in% c("classic", "electric") & contrast %in% c("rain_residential", "rain_mixed", "rain_commercial", "hot_residential", "hot_mixed", "hot_commercial")]
fwrite(veh, file.path(out_dir, "vehicle_effects.csv"))
if (nrow(veh) && all(is.finite(veh$pct))) {
  veh[, c("stress", "place") := tstrsplit(contrast, "_", fixed = TRUE)]
  p3 <- ggplot(veh, aes(place, pct, colour = outcome)) +
    geom_hline(yintercept = 0, linetype = 2) +
    geom_pointrange(aes(ymin = pct_lo, ymax = pct_hi), position = position_dodge(width = 0.4)) +
    facet_wrap(~stress, scales = "free_y") +
    scale_colour_manual(values = c("classic" = "#0072B2", "electric" = "#D55E00"),
                        labels = c("Classic bike", "E-bike")) +
    labs(x = NULL, y = "Change in starts (%)", colour = NULL) +
    theme_minimal(base_size = 12)
  ggsave(file.path(fig_dir, "vehicle_effects.png"), p3, width = 8, height = 4.2, dpi = 220)
  ggsave(file.path(out_dir, "vehicle_effects.png"), p3, width = 8, height = 4.2, dpi = 220)
}

month_dt <- base[, .(starts = mean(starts), rain = mean(rain), temp = mean(temp)), by = month]
fwrite(month_dt, file.path(out_dir, "monthly_means.csv"))
p4 <- ggplot(month_dt, aes(month, starts)) +
  geom_line(linewidth = 0.7) + geom_point() +
  scale_x_continuous(breaks = 1:12) +
  labs(x = "Month", y = "Mean starts per station-period") +
  theme_minimal(base_size = 12)
ggsave(file.path(fig_dir, "monthly_starts.png"), p4, width = 7, height = 3.6, dpi = 220)

cat("estimation complete\n")
