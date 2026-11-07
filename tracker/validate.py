"""Replay validation: run the estimator headless and compare with real car positions.

Ground truth: Position.z from the F1 archive or, while that needs a login, OpenF1
`location` (the same x/y signal). Truth is projected onto the track centreline;
error = signed along-track distance (estimate - truth), wrapped to +-L/2.
Excluded: cars in the pit lane (truth > 15 m from the centreline or inside a pit
window), cars the estimator shows as pit/out/grid, and time after the leader finishes.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

import numpy as np

from .engine import Engine
from .track import Track

log = logging.getLogger(__name__)
OUT_DIR = Path(__file__).resolve().parent.parent / "validation"
STEP = 0.5          # s between comparisons
OFF_TRACK_M = 15.0  # truth further than this from the centreline = pit lane / off


def run_validation(msgs, meta, src, track: Track, args) -> int:
    t_start = meta.get("t_start") or next(m[0] for m in msgs if m[1] == "SessionStatus"
                                          and (m[2] or {}).get("Status") == "Started")
    t_end = meta.get("t_finish") or msgs[-1][0]
    t0 = t_start - 60

    log.info("loading ground truth (%s)…", src)
    if src == "archive":
        from .sources import archive
        truth = archive.load_positions(meta["path"])
    elif src == "openf1":
        from .sources import openf1_replay
        truth = openf1_replay.load_positions(meta["session_key"], meta["drivers"], t0, t_end + 30)
    else:
        log.error("validation needs --source archive or openf1 (a recording has no ground truth)")
        return 1
    truth_arr = {c: np.array(v) for c, v in truth.items() if len(v) > 10}

    eng = Engine(track)
    i = 0
    while i < len(msgs) and (msgs[i][0] <= t0 or msgs[i][3]):
        eng.ingest(*msgs[i])
        i += 1
    eng.start(t0)
    pit_win = {str(k): v for k, v in (meta.get("pit_windows") or {}).items()}
    L = track.length
    last_s: dict[str, float] = {}
    rows = []  # (t, car, leader_lap, err)
    t = t0
    while t < t_end:
        t += STEP
        while i < len(msgs) and msgs[i][0] <= t:
            eng.ingest(*msgs[i])
            i += 1
        eng.tick(t)
        if t < t_start:
            continue
        snap = eng.snapshot()
        lap = (snap.get("lap") or _leader_lap(eng)) or 0
        for c in snap["cars"]:
            num = c["num"]
            if c["mode"] != "track" or num not in truth_arr:
                continue
            if any(a - 5 <= t <= b + 5 for a, b in pit_win.get(num, [])):
                continue
            tr = truth_arr[num]
            j = np.searchsorted(tr[:, 0], t)
            if j <= 0 or j >= len(tr) or tr[j, 0] - tr[j - 1, 0] > 2.0:
                continue
            u = (t - tr[j - 1, 0]) / (tr[j, 0] - tr[j - 1, 0])
            x = tr[j - 1, 1] + u * (tr[j, 1] - tr[j - 1, 1])
            y = tr[j - 1, 2] + u * (tr[j, 2] - tr[j - 1, 2])
            s_true, d = track.project(x, y, last_s.get(num))
            if d > 60 and num in last_s:  # lost the local search; do a global one
                s_true, d = track.project(x, y)
            last_s[num] = s_true
            if d > OFF_TRACK_M:
                continue
            err = (c["D"] - s_true + L / 2) % L - L / 2
            rows.append((t, num, lap, err))

    if not rows:
        log.error("no comparable samples")
        return 1
    return report(rows, track, src, args)


def _leader_lap(eng: Engine) -> int:
    for num, line in eng.state.lines.items():
        if str(line.get("Position")) == "1":
            return eng.state.laps_done(num) + 1
    return 0


def report(rows, track: Track, src: str, args) -> int:
    err = np.array([r[3] for r in rows])
    ab = np.abs(err)
    by_lap = defaultdict(list)
    by_car = defaultdict(list)
    for t, car, lap, e in rows:
        by_lap[int(lap)].append(abs(e))
        by_car[car].append(e)
    print(f"\nValidation: {args.year} {args.event} {args.session}  (source: {src}, "
          f"truth: {'Position.z' if src == 'archive' else 'OpenF1 location'}, track {track.name})")
    print(f"samples: {len(err)} (every {STEP}s per car on track)")
    print(f"{'lap':>4} {'n':>6} {'mean m':>8} {'p95 m':>8} {'max m':>8}")
    laps = sorted(by_lap)
    stats = []
    for lap in laps:
        a = np.array(by_lap[lap])
        stats.append((lap, len(a), float(a.mean()), float(np.percentile(a, 95)), float(a.max())))
        print(f"{lap:>4} {len(a):>6} {a.mean():>8.1f} {np.percentile(a, 95):>8.1f} {a.max():>8.1f}")
    overall = {"mean": float(ab.mean()), "median": float(np.median(ab)), "p95": float(np.percentile(ab, 95)),
               "max": float(ab.max()), "bias": float(err.mean()),
               "within_50m": float((ab < 50).mean()), "within_100m": float((ab < 100).mean())}
    print(f"\nALL  mean {overall['mean']:.1f} m  median {overall['median']:.1f} m  p95 {overall['p95']:.1f} m  "
          f"max {overall['max']:.1f} m  bias {overall['bias']:+.1f} m  "
          f"<50 m {100 * overall['within_50m']:.0f}%  <100 m {100 * overall['within_100m']:.0f}%")
    cars = sorted(by_car, key=lambda c: np.mean(np.abs(by_car[c])))
    print("per car mean |err| m: " + ", ".join(f"{c}:{np.mean(np.abs(by_car[c])):.0f}" for c in cars))

    OUT_DIR.mkdir(exist_ok=True)
    name = f"{args.year}_{track.name}_{args.session.lower()}"
    (OUT_DIR / f"{name}.json").write_text(json.dumps(
        {"overall": overall, "per_lap": stats, "source": src,
         "per_car_mean": {c: float(np.mean(np.abs(v))) for c, v in by_car.items()}}, indent=1))
    plot(stats, by_car, OUT_DIR / f"{name}.png", f"{args.year} {args.event} {args.session}")
    print(f"wrote {OUT_DIR / name}.png / .json")
    return 0


def plot(stats, by_car, path: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, grid = "#1f2328", "#5f6670", "#e6e6e3"
    blues = {"max": "#9ec5f4", "p95": "#3987e5", "mean": "#104281"}
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.8), gridspec_kw={"width_ratios": [2.3, 1]})
    laps = [s[0] for s in stats]
    for key, idx, lw in (("max", 4, 1.2), ("p95", 3, 2), ("mean", 2, 2)):
        ys = [s[idx] for s in stats]
        a1.plot(laps, ys, color=blues[key], lw=lw)
        a1.annotate(key, (laps[-1], ys[-1]), xytext=(6, 0), textcoords="offset points",
                    color=ink, va="center", fontsize=9)
    a1.set_title(f"Along-track position error per lap — {title}", loc="left", color=ink, fontsize=11)
    a1.set_xlabel("leader lap", color=muted)
    a1.set_ylabel("|error| (m)", color=muted)
    a1.set_ylim(bottom=0)
    cars = sorted(by_car, key=lambda c: np.mean(np.abs(by_car[c])))
    vals = [np.mean(np.abs(by_car[c])) for c in cars]
    a2.barh(range(len(cars)), vals, color="#3987e5", height=0.7)
    a2.set_yticks(range(len(cars)), [f"#{c}" for c in cars], fontsize=8)
    a2.invert_yaxis()
    a2.set_title("Mean |error| per car (m)", loc="left", color=ink, fontsize=11)
    for ax in (a1, a2):
        ax.grid(axis="y" if ax is a1 else "x", color=grid, lw=0.8)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(grid)
        ax.tick_params(colors=muted, labelsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
