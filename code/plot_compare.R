#!/usr/bin/env Rscript
suppressPackageStartupMessages({library(data.table); library(ggplot2)})
cmd <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", cmd[grep("^--file=", cmd)])
root <- normalizePath(file.path(dirname(file_arg[[1]]), ".."), mustWork = TRUE)
fig <- file.path(root, "output/figures")
out <- file.path(root, "output")
dt <- fread(file.path(out, "compare_effects.csv"))
# First copy of each key is the published New York sample or the London tube sample.
dt <- dt[, .SD[1], by = .(city, outcome, contrast)]
want <- data.table(
  outcome = c("starts","starts","starts","starts","classic","electric","access","access"),
  contrast = c("rain","rain_morning","rain_midday","rain1","rain","rain","rain_within_800","rain_beyond_800"),
  label = c("Rainy period","Morning","Midday","At least 1 mm","Classic bike","E-bike","Within 800 m","Beyond 800 m")
)
# rain1 is outcome starts contrast rain1. Good.
plot_dt <- merge(dt, want, by = c("outcome","contrast"))
plot_dt[, label := factor(label, levels = rev(c(
  "Rainy period","At least 1 mm","Morning","Midday","Classic bike","E-bike","Within 800 m","Beyond 800 m")))]
plot_dt[, city := factor(city, levels = c("New York","London"))]
fwrite(plot_dt[, .(city, label, pct, pct_lo, pct_hi)], file.path(out, "compare_plot_data.csv"))
p <- ggplot(plot_dt, aes(pct, label, colour = city)) +
  geom_vline(xintercept = 0, linetype = 2, colour = "grey40") +
  geom_errorbarh(aes(xmin = pct_lo, xmax = pct_hi), height = 0.18,
                 position = position_dodge(width = 0.55), linewidth = 0.45) +
  geom_point(position = position_dodge(width = 0.55), size = 2.2) +
  scale_colour_manual(values = c("New York" = "#0072B2", "London" = "#D55E00")) +
  labs(x = "Change in trip starts (%)", y = NULL, colour = NULL) +
  theme_minimal(base_size = 12) +
  theme(legend.position = "bottom", panel.grid.minor = element_blank())
ggsave(file.path(fig, "city_comparison.png"), p, width = 7.4, height = 5.0, dpi = 220)
ggsave(file.path(out, "city_comparison.png"), p, width = 7.4, height = 5.0, dpi = 220)
cat("figure written\n")
