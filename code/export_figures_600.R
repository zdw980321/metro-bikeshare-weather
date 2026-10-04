#!/usr/bin/env Rscript
suppressPackageStartupMessages({library(data.table); library(ggplot2)})
cmd <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", cmd[grep("^--file=", cmd)])
root <- normalizePath(file.path(dirname(file_arg[[1]]), ".."), mustWork = TRUE)
fig <- file.path(root, "output/figures")
out <- file.path(root, "output")
dir.create(fig, recursive = TRUE, showWarnings = FALSE)

theme_pub <- function(base = 12) {
  theme_minimal(base_size = base) +
    theme(
      text = element_text(colour = "black"),
      axis.text = element_text(colour = "black"),
      axis.title = element_text(colour = "black"),
      legend.text = element_text(colour = "black"),
      strip.text = element_text(colour = "black"),
      panel.grid.minor = element_blank(),
      panel.grid.major = element_line(colour = "grey85"),
      plot.background = element_rect(fill = "white", colour = NA),
      panel.background = element_rect(fill = "white", colour = NA)
    )
}
save_png <- function(plot, name, width, height) {
  ggsave(file.path(fig, name), plot, width = width, height = height, dpi = 600, bg = "white")
  ggsave(file.path(out, name), plot, width = width, height = height, dpi = 600, bg = "white")
}

st <- fread(file.path(root, "data/processed/stations.csv"))
map_dt <- st[access_800 == 1]
map_dt[, place := factor(place_type, levels = c("residential", "mixed", "commercial", "other", "unclassified"))]
map_dt[place == "unclassified", place := "other"]
map_dt[, place := factor(place, levels = c("residential", "mixed", "commercial", "other"))]
p_map <- ggplot(map_dt, aes(lng, lat, colour = place)) +
  geom_point(size = 1.15, alpha = 0.9) +
  scale_colour_manual(values = c(
    residential = "#0072B2", mixed = "#009E73", commercial = "#D55E00", other = "grey55"
  ), labels = c("Residential", "Mixed", "Commercial", "Other")) +
  coord_equal() +
  labs(x = "Longitude", y = "Latitude", colour = NULL) +
  theme_pub(11) +
  theme(legend.position = "bottom")
save_png(p_map, "station_map.png", 7.4, 7.6)

means <- fread(file.path(out, "starts_by_rain_place.csv"))
means <- means[place_type %in% c("residential", "mixed", "commercial")]
means[, place_type := factor(place_type, levels = c("residential", "mixed", "commercial"),
                              labels = c("Residential", "Mixed", "Commercial"))]
means[, period := factor(period, levels = c("night", "morning", "midday", "evening", "late"),
                          labels = c("Night", "Morning", "Midday", "Evening", "Late"))]
means[, rain := factor(rain, levels = c(0, 1), labels = c("No rain", "Rain"))]
p_means <- ggplot(means, aes(period, starts, fill = rain)) +
  geom_col(position = "dodge", width = 0.72, colour = "black", linewidth = 0.15) +
  facet_wrap(~place_type) +
  scale_fill_manual(values = c("No rain" = "#0072B2", "Rain" = "#E69F00")) +
  labs(x = NULL, y = "Mean trip starts per station-period", fill = NULL) +
  theme_pub(12) +
  theme(axis.text.x = element_text(angle = 30, hjust = 1), legend.position = "bottom")
save_png(p_means, "starts_by_rain_place.png", 9.0, 4.8)

eff <- fread(file.path(out, "percent_effects.csv"))
rain_plot <- eff[outcome == "starts_het" & contrast %in% c("rain_residential", "rain_mixed", "rain_commercial")]
rain_plot[, place := factor(sub("^rain_", "", contrast),
                             levels = c("commercial", "mixed", "residential"),
                             labels = c("Commercial", "Mixed", "Residential"))]
p_rain <- ggplot(rain_plot, aes(pct, place)) +
  geom_vline(xintercept = 0, linetype = 2, colour = "grey35") +
  geom_errorbarh(aes(xmin = pct_lo, xmax = pct_hi), height = 0.18, colour = "black", linewidth = 0.45) +
  geom_point(size = 2.4, colour = "black") +
  labs(x = "Change in trip starts during rain (%)", y = NULL) +
  theme_pub(12)
save_png(p_rain, "rain_effects.png", 7.2, 3.5)

veh <- fread(file.path(out, "vehicle_effects.csv"))
veh <- veh[contrast %in% c("rain_residential", "rain_mixed", "rain_commercial",
                           "hot_residential", "hot_mixed", "hot_commercial")]
veh[, c("stress", "place") := tstrsplit(contrast, "_", fixed = TRUE)]
veh[, stress := factor(stress, levels = c("rain", "hot"), labels = c("Rain", "Heat"))]
veh[, place := factor(place, levels = c("residential", "mixed", "commercial"),
                       labels = c("Residential", "Mixed", "Commercial"))]
veh[, fleet := factor(outcome, levels = c("classic", "electric"),
                       labels = c("Classic bike", "E-bike"))]
p_veh <- ggplot(veh, aes(place, pct, colour = fleet)) +
  geom_hline(yintercept = 0, linetype = 2, colour = "grey35") +
  geom_pointrange(aes(ymin = pct_lo, ymax = pct_hi),
                  position = position_dodge(width = 0.45), linewidth = 0.45) +
  facet_wrap(~stress, scales = "free_y") +
  scale_colour_manual(values = c("Classic bike" = "#0072B2", "E-bike" = "#D55E00")) +
  labs(x = NULL, y = "Change in starts (%)", colour = NULL) +
  theme_pub(12) +
  theme(legend.position = "bottom")
save_png(p_veh, "vehicle_effects.png", 8.0, 4.3)

cmp <- fread(file.path(out, "compare_effects.csv"))
cmp <- cmp[, .SD[1], by = .(city, outcome, contrast)]
want <- data.table(
  outcome = c("starts", "starts", "starts", "starts", "classic", "electric", "access", "access"),
  contrast = c("rain", "rain_morning", "rain_midday", "rain1", "rain", "rain", "rain_within_800", "rain_beyond_800"),
  label = c("Rainy period", "Morning", "Midday", "At least 1 mm", "Classic bike", "E-bike", "Within 800 m", "Beyond 800 m")
)
plot_dt <- merge(cmp, want, by = c("outcome", "contrast"))
plot_dt[, label := factor(label, levels = rev(c(
  "Rainy period", "At least 1 mm", "Morning", "Midday", "Classic bike", "E-bike", "Within 800 m", "Beyond 800 m"
)))]
plot_dt[, city := factor(city, levels = c("New York", "London"))]
p_cmp <- ggplot(plot_dt, aes(pct, label, colour = city)) +
  geom_vline(xintercept = 0, linetype = 2, colour = "grey35") +
  geom_errorbarh(aes(xmin = pct_lo, xmax = pct_hi), height = 0.18,
                 position = position_dodge(width = 0.55), linewidth = 0.45) +
  geom_point(position = position_dodge(width = 0.55), size = 2.2) +
  scale_colour_manual(values = c("New York" = "#0072B2", "London" = "#D55E00")) +
  labs(x = "Change in trip starts (%)", y = NULL, colour = NULL) +
  theme_pub(12) +
  theme(legend.position = "bottom")
save_png(p_cmp, "city_comparison.png", 7.4, 5.1)
cat("600 dpi figures written\n")
