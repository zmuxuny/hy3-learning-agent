"""Standalone report figures from the recomputed E7 JSON (matplotlib 3.10.6)."""

import argparse
import json
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--summary", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
d = json.loads(a.summary.read_text())
a.output.mkdir(parents=True, exist_ok=True)
tracks = ["planning", "intervention", "assessment", "revision"]
colors = {
    "improved": "#287c67",
    "regressed": "#b54442",
    "tied": "#737d89",
    "unscored_pair": "#b9bec5",
}
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "svg.fonttype": "none",
        "savefig.facecolor": "white",
    }
)


def save(fig, name):
    for suffix in ["svg", "png", "pdf"]:
        fig.savefig(
            a.output / f"{name}.{suffix}",
            dpi=180,
            bbox_inches="tight",
            metadata={"Creator": "Learning Agent E7"},
        )
    plt.close(fig)


fig, axes = plt.subplots(1, 4, figsize=(13, 4), sharey=True)
for ax, track in zip(axes, tracks):
    pairs = [r for r in d["product"]["pairs"] if r["track"] == track]
    for offset, r in enumerate(pairs):
        if r["valid_pair"]:
            ax.plot(
                [0, 1],
                [r["baseline_score"], r["candidate_score"]],
                marker="o",
                lw=1.5,
                alpha=0.85,
                color=colors[r["change"]],
            )
            ax.annotate(
                r["case_id"].split("-")[1].upper(),
                (1, r["candidate_score"]),
                xytext=(5, (offset - 1.5) * 9),
                textcoords="offset points",
                fontsize=8,
            )
    valid = sum(r["valid_pair"] for r in pairs)
    ax.set_title(f"{track.title()}\n{valid}/{len(pairs)} scored pairs")
    ax.set_xticks([0, 1], ["Baseline", "Candidate"])
    ax.set_xlim(-0.15, 1.4)
    ax.set_ylim(-3, 105)
    ax.grid(axis="y", alpha=0.15)
axes[0].set_ylabel("Reviewed score (0–100)")
fig.suptitle("Same cases, same Judge method · all failures retained", y=1.04)
fig.text(
    0.5,
    -0.04,
    "Green: higher score   Red: lower score   Gray: equal score   Missing pairs excluded from lines, included in denominators",
    ha="center",
    fontsize=9,
)
save(fig, "paired-scores")

matrix = np.array(
    [
        [
            d["product"]["tracks"][t]["dimension_delta_means"].get(f"D{i}", np.nan)
            for i in range(1, 8)
        ]
        for t in tracks
    ]
)
fig, ax = plt.subplots(figsize=(9, 3.5))
im = ax.imshow(matrix, cmap="RdYlGn", vmin=-2, vmax=2, aspect="auto")
ax.set_yticks(range(4), [t.title() for t in tracks])
ax.set_xticks(range(7), [f"D{i}" for i in range(1, 8)])
for i in range(4):
    for j in range(7):
        ax.text(
            j,
            i,
            "—" if np.isnan(matrix[i, j]) else f"{matrix[i, j]:+.2f}",
            ha="center",
            va="center",
        )
ax.set_title("Candidate − Baseline · mean dimension levels on valid pairs")
fig.colorbar(im, ax=ax, label="Level change (0/1/2 scale)", shrink=0.85)
save(fig, "dimension-deltas")

fig, axes = plt.subplots(1, 3, figsize=(11, 3.4), sharey=True)
for ax, label in zip(axes, ["good", "mild", "severe"]):
    selected = [r for r in d["method_stability"] if r["label"] == label]
    for arm, x, c in [("baseline", 0, "#737d89"), ("candidate", 1, "#287c67")]:
        rows = [r for r in selected if r["arm"] == arm]
        for i, row in enumerate(rows):
            sd = row["score_population_sd"]
            if sd is not None:
                ax.scatter(x + (i - 1.5) * 0.08, sd, color=c, s=35)
    ax.set_title(label.title())
    ax.set_xticks([0, 1], ["Old Judge", "New Judge"])
    ax.set_xlim(-0.4, 1.4)
    ax.grid(axis="y", alpha=0.15)
axes[0].set_ylabel("Population score SD (two repeats)")
fig.suptitle("Identical controlled trajectories · four cases per grade", y=1.04)
save(fig, "method-stability")
