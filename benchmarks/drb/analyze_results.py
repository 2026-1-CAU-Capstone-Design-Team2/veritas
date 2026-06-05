"""Compare two systems' DRB evaluator outputs (RACE + FACT).

Reads the official evaluator outputs for two model names and produces a paired
comparison:

* **RACE** per-task ``overall_score`` (and the four dimension scores) from
  ``results/race/<model>/raw_results.jsonl`` — paired by task ``id``.
* **FACT** aggregates from ``results/fact/<model>/fact_result.txt``.

Outputs under ``bench_results/drb/<comparison>/``:
* ``summary.csv`` — per-system means + FACT aggregates,
* ``paired_deltas.csv`` — per-task A−B deltas,
* ``comparison_report.md`` — mean/median delta, win rate, bootstrap 95% CI.

If per-task RACE files are unavailable it degrades to **aggregate-only** mode
(``race_result.txt`` means, no paired deltas/CI) and says so in the report.

All statistics are deterministic — the bootstrap uses a fixed seed. The score
labels are **budget_judge** or **official_judge_confirmation** depending on which
evaluator models were configured; this tool does not assign that label itself —
pass it via ``--label`` so the report is never mistaken for a leaderboard score.

Pure analysis (stdlib only); no network, no LLM.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import statistics
from pathlib import Path
from typing import Any

from benchmarks.drb import drb_vendor
from benchmarks.drb.drb_io import iter_json_objects


_RACE_DIMS = ("comprehensiveness", "insight", "instruction_following", "readability")
_RACE_METRICS = ("overall_score",) + _RACE_DIMS

_RACE_TXT_KEYS = {
    "comprehensiveness": "comprehensiveness",
    "insight": "insight",
    "instruction following": "instruction_following",
    "readability": "readability",
    "overall score": "overall_score",
}


# --------------------------------------------------------------------------
# Parsing (each tolerant of a missing file → empty)
# --------------------------------------------------------------------------
def parse_race_per_task(raw_results_path: str | Path) -> dict[str, dict[str, float]]:
    """``results/race/<model>/raw_results.jsonl`` → ``{id: {metric: score}}``.

    Rows carrying an ``error`` are skipped. Missing file → empty dict.
    """
    path = Path(raw_results_path)
    if not path.is_file():
        return {}
    out: dict[str, dict[str, float]] = {}
    for row in iter_json_objects(path.read_text(encoding="utf-8")):
        if "id" not in row or "error" in row:
            continue
        scores = {m: _as_float(row.get(m)) for m in _RACE_METRICS if row.get(m) is not None}
        if scores:
            out[str(row["id"])] = scores
    return out


def parse_label_value_txt(txt_path: str | Path, key_map: dict[str, str] | None = None) -> dict[str, float]:
    """Parse a ``Label: value`` results .txt into ``{normalized_key: float}``."""
    path = Path(txt_path)
    if not path.is_file():
        return {}
    out: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        raw_key, raw_val = line.split(":", 1)
        key = raw_key.strip().lower()
        if key_map is not None:
            key = key_map.get(key, key)
        value = _as_float(raw_val)
        if value is not None:
            out[key] = value
    return out


def parse_race_aggregate(race_result_txt: str | Path) -> dict[str, float]:
    return parse_label_value_txt(race_result_txt, _RACE_TXT_KEYS)


def parse_fact_aggregate(fact_result_txt: str | Path) -> dict[str, float]:
    return parse_label_value_txt(fact_result_txt)


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Statistics (pure)
# --------------------------------------------------------------------------
def paired_deltas(
    a_by_id: dict[str, dict[str, float]],
    b_by_id: dict[str, dict[str, float]],
    metric: str = "overall_score",
) -> list[tuple[str, float, float, float]]:
    """``(id, a, b, a-b)`` for every task scored by **both** systems, id-sorted."""
    shared = sorted(set(a_by_id) & set(b_by_id), key=lambda x: (len(x), x))
    deltas: list[tuple[str, float, float, float]] = []
    for task_id in shared:
        a_val = a_by_id[task_id].get(metric)
        b_val = b_by_id[task_id].get(metric)
        if a_val is None or b_val is None:
            continue
        deltas.append((task_id, a_val, b_val, a_val - b_val))
    return deltas


def bootstrap_ci(
    values: list[float], *, seed: int = 12345, n_boot: int = 2000, alpha: float = 0.05
) -> tuple[float, float]:
    """Deterministic percentile bootstrap CI of the mean (fixed seed)."""
    n = len(values)
    if n == 0:
        return (float("nan"), float("nan"))
    if n == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        sample_sum = 0.0
        for _ in range(n):
            sample_sum += values[rng.randrange(n)]
        means.append(sample_sum / n)
    means.sort()
    lo_idx = max(0, int((alpha / 2) * n_boot))
    hi_idx = min(n_boot - 1, int((1 - alpha / 2) * n_boot))
    return (means[lo_idx], means[hi_idx])


def summarize_deltas(
    deltas: list[tuple[str, float, float, float]], *, seed: int = 12345, n_boot: int = 2000
) -> dict[str, Any]:
    """Mean/median delta, win/loss/tie counts, win rate, and bootstrap CI."""
    diffs = [d for (_id, _a, _b, d) in deltas]
    n = len(diffs)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for d in diffs if d > 0)
    losses = sum(1 for d in diffs if d < 0)
    ties = sum(1 for d in diffs if d == 0)
    ci_low, ci_high = bootstrap_ci(diffs, seed=seed, n_boot=n_boot)
    return {
        "n": n,
        "mean_delta": statistics.fmean(diffs),
        "median_delta": statistics.median(diffs),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": wins / n,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "seed": seed,
        "n_boot": n_boot,
    }


def _metric_means(by_id: dict[str, dict[str, float]]) -> dict[str, float]:
    means: dict[str, float] = {}
    for metric in _RACE_METRICS:
        vals = [s[metric] for s in by_id.values() if metric in s]
        if vals:
            means[metric] = statistics.fmean(vals)
    return means


# --------------------------------------------------------------------------
# Path helpers + high-level analyze
# --------------------------------------------------------------------------
def _race_dir(drb_root: Path, model: str) -> Path:
    return drb_root / "results" / "race" / model


def _fact_dir(drb_root: Path, model: str) -> Path:
    return drb_root / "results" / "fact" / model


def analyze(
    *,
    drb_root: str | Path,
    system_a: str,
    system_b: str,
    seed: int = 12345,
    n_boot: int = 2000,
) -> dict[str, Any]:
    """Build the full comparison structure for two model names."""
    root = drb_vendor.resolve_drb_root(drb_root)
    a_race = parse_race_per_task(_race_dir(root, system_a) / "raw_results.jsonl")
    b_race = parse_race_per_task(_race_dir(root, system_b) / "raw_results.jsonl")

    per_task_available = bool(a_race) and bool(b_race)
    deltas = paired_deltas(a_race, b_race) if per_task_available else []
    summary = summarize_deltas(deltas, seed=seed, n_boot=n_boot) if deltas else {"n": 0}

    return {
        "system_a": system_a,
        "system_b": system_b,
        "mode": "paired" if deltas else "aggregate_only",
        "per_task_available": per_task_available,
        "race_means_a": _metric_means(a_race) if a_race else parse_race_aggregate(_race_dir(root, system_a) / "race_result.txt"),
        "race_means_b": _metric_means(b_race) if b_race else parse_race_aggregate(_race_dir(root, system_b) / "race_result.txt"),
        "fact_a": parse_fact_aggregate(_fact_dir(root, system_a) / "fact_result.txt"),
        "fact_b": parse_fact_aggregate(_fact_dir(root, system_b) / "fact_result.txt"),
        "deltas": deltas,
        "summary": summary,
    }


def write_outputs(result: dict[str, Any], out_dir: str | Path, *, label: str = "unlabeled") -> dict[str, Path]:
    """Write summary.csv / paired_deltas.csv / comparison_report.md."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary_csv = out / "summary.csv"
    deltas_csv = out / "paired_deltas.csv"
    report_md = out / "comparison_report.md"

    a, b = result["system_a"], result["system_b"]
    means_a, means_b = result["race_means_a"], result["race_means_b"]

    with summary_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", a, b, "delta(a-b)"])
        for metric in _RACE_METRICS:
            va, vb = means_a.get(metric), means_b.get(metric)
            delta = (va - vb) if (va is not None and vb is not None) else ""
            writer.writerow([metric, _fmt(va), _fmt(vb), _fmt(delta)])
        for key in sorted(set(result["fact_a"]) | set(result["fact_b"])):
            va, vb = result["fact_a"].get(key), result["fact_b"].get(key)
            delta = (va - vb) if (va is not None and vb is not None) else ""
            writer.writerow([f"fact:{key}", _fmt(va), _fmt(vb), _fmt(delta)])

    with deltas_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["task_id", f"{a}_overall", f"{b}_overall", "delta(a-b)"])
        for task_id, a_val, b_val, delta in result["deltas"]:
            writer.writerow([task_id, _fmt(a_val), _fmt(b_val), _fmt(delta)])

    report_md.write_text(_render_report(result, label=label), encoding="utf-8")
    return {"summary_csv": summary_csv, "paired_deltas_csv": deltas_csv, "comparison_report_md": report_md}


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _render_report(result: dict[str, Any], *, label: str) -> str:
    a, b = result["system_a"], result["system_b"]
    summary = result["summary"]
    lines = [
        f"# DRB comparison: {a} vs {b}",
        "",
        f"- Score label: **{label}** (not an official DRB leaderboard score unless explicitly the official-judge run)",
        f"- Mode: **{result['mode']}**"
        + ("" if result["per_task_available"] else " — per-task RACE files missing; aggregate means only"),
        "",
        "## RACE means (higher is better; overall_score is target/(target+reference))",
        "",
        "| metric | " + a + " | " + b + " | delta (a−b) |",
        "|---|---|---|---|",
    ]
    for metric in _RACE_METRICS:
        va, vb = result["race_means_a"].get(metric), result["race_means_b"].get(metric)
        delta = (va - vb) if (va is not None and vb is not None) else None
        lines.append(f"| {metric} | {_fmt(va)} | {_fmt(vb)} | {_fmt(delta)} |")

    lines += ["", "## Paired RACE overall_score (A − B)", ""]
    if summary.get("n"):
        lines += [
            f"- Paired tasks: {summary['n']}",
            f"- Mean delta: {_fmt(summary['mean_delta'])}",
            f"- Median delta: {_fmt(summary['median_delta'])}",
            f"- Win rate (A > B): {_fmt(summary['win_rate'])} "
            f"(W{summary['wins']}/L{summary['losses']}/T{summary['ties']})",
            f"- Bootstrap 95% CI of mean delta: "
            f"[{_fmt(summary['ci_low'])}, {_fmt(summary['ci_high'])}] "
            f"(seed={summary['seed']}, n_boot={summary['n_boot']})",
        ]
    else:
        lines.append("- No paired per-task scores available (aggregate-only mode).")

    lines += ["", "## FACT (citation trustworthiness)", "", "| metric | " + a + " | " + b + " |", "|---|---|---|"]
    for key in sorted(set(result["fact_a"]) | set(result["fact_b"])):
        lines.append(f"| {key} | {_fmt(result['fact_a'].get(key))} | {_fmt(result['fact_b'].get(key))} |")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two systems' DRB RACE/FACT outputs.")
    parser.add_argument("--drb-root", default=drb_vendor.DEFAULT_DRB_ROOT)
    parser.add_argument("--system-a", required=True, help="Model name (e.g. veritas_autosurvey_local_m15).")
    parser.add_argument("--system-b", required=True, help="Model name (e.g. flat_local_web_m15).")
    parser.add_argument("--comparison", default=None, help="Output subdir name (default: <a>__vs__<b>).")
    parser.add_argument("--out", default=None, help="Output dir (default: bench_results/drb/<comparison>).")
    parser.add_argument("--label", default="unlabeled", help="budget_judge | official_judge_confirmation | unlabeled")
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--n-boot", type=int, default=2000)
    args = parser.parse_args(argv)

    comparison = args.comparison or f"{args.system_a}__vs__{args.system_b}"
    out_dir = Path(args.out) if args.out else Path("bench_results/drb") / comparison

    result = analyze(
        drb_root=args.drb_root,
        system_a=args.system_a,
        system_b=args.system_b,
        seed=args.seed,
        n_boot=args.n_boot,
    )
    written = write_outputs(result, out_dir, label=args.label)
    print(f"[drb][analyze] mode={result['mode']} label={args.label}")
    for name, path in written.items():
        print(f"[drb][analyze] {name}: {path}")
    if result["summary"].get("n"):
        s = result["summary"]
        print(
            f"[drb][analyze] mean_delta={s['mean_delta']:.4f} "
            f"win_rate={s['win_rate']:.3f} "
            f"CI=[{s['ci_low']:.4f}, {s['ci_high']:.4f}] (n={s['n']})"
        )
    return 0


__all__ = [
    "parse_race_per_task",
    "parse_race_aggregate",
    "parse_fact_aggregate",
    "parse_label_value_txt",
    "paired_deltas",
    "bootstrap_ci",
    "summarize_deltas",
    "analyze",
    "write_outputs",
]


if __name__ == "__main__":
    raise SystemExit(main())
