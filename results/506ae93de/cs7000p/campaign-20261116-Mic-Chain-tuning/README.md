# Mic chain second-pass tuning, cs7000p model, OpenRTX 506ae93de

Exploratory comparison of the AGC target (full at -27/-24/-21/-18 dBFS) and of pause handling
(full_exp: 20 dB expander; full_sil: codec2 3200 silence frame) against baseline and full.
Report: [REPORT.md](REPORT.md); methods: [METHODS.md](METHODS.md); tables in `data/`, charts in `charts/`.

The individual runs were not exported (they stay in the tester's take cache and are
regenerated from the recipes, seeds and firmware recorded in `run.json`).
