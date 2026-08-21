#!/usr/bin/env python3
"""
Aggregate SWE-bench baseline results by domain (repo).

Reads the execution results.json (token usage + execution time) and the
local_swe_evaluator evaluation_results.json (resolution status), joins them
by instance_id, and produces per-domain aggregates:

    - Accuracy (FULL / total)
    - Average execution time (seconds)
    - Average token usage (total_tokens)

Usage:
    python scripts/aggregate_swebench_results.py \
        --results-dir output_paper/swe_bench_verified_baseline_eval \
        --evaluation output_paper/swe_bench_verified_baseline_eval/evaluation_results.json \
        --cases-file output_paper/verified_selected_cases.jsonl \
        --output output_paper/swe_bench_verified_baseline_eval/domain_summary.json
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def get_repo_from_instance_id(instance_id: str) -> str:
    """Extract repo name from instance_id (e.g. 'django__django-15930' -> 'django')."""
    parts = instance_id.split("__")
    return parts[0] if parts else instance_id


def find_results_json(results_dir: Path) -> Path:
    """Find the results.json file under the results directory."""
    # Layout: <results_dir>/<dataset>/<config_name>/results.json
    candidates = list(results_dir.rglob("results.json"))
    if not candidates:
        # Fallback: results.json directly in results_dir
        direct = results_dir / "results.json"
        if direct.exists():
            return direct
        raise FileNotFoundError(f"No results.json found under {results_dir}")
    # Prefer the one with the most entries
    best = max(candidates, key=lambda p: p.stat().st_size)
    return best


def load_results_json(results_dir: Path) -> dict:
    """Load execution results.json, return dict keyed by instance_id."""
    results_path = find_results_json(results_dir)
    print(f"Loading execution results from: {results_path}")
    with open(results_path) as f:
        data = json.load(f)

    if not isinstance(data, list):
        print(f"WARNING: results.json is not a list (type={type(data).__name__})")
        return {}

    by_id = {}
    for entry in data:
        tid = entry.get("task_id")
        if tid:
            by_id[tid] = entry
    print(f"  Loaded {len(by_id)} task entries")
    return by_id


def load_evaluation(eval_path: Path) -> dict:
    """Load evaluation_results.json, return dict keyed by instance_id."""
    if not eval_path.exists():
        print(f"WARNING: evaluation file not found: {eval_path}")
        return {}

    print(f"Loading evaluation results from: {eval_path}")
    try:
        with open(eval_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"WARNING: failed to parse evaluation file ({e}), treating as empty")
        return {}

    results = data.get("results", data) if isinstance(data, dict) else data
    if not isinstance(results, list):
        return {}

    by_id = {}
    for entry in results:
        tid = entry.get("instance_id") or entry.get("task_id")
        if tid:
            by_id[tid] = entry
    print(f"  Loaded {len(by_id)} evaluation entries")
    return by_id


def main():
    parser = argparse.ArgumentParser(description="Aggregate SWE-bench results by domain")
    parser.add_argument("--results-dir", required=True,
                        help="Directory containing results.json with token/time data")
    parser.add_argument("--evaluation", required=True,
                        help="evaluation_results.json from local_swe_evaluator")
    parser.add_argument("--cases-file", default=None,
                        help="jsonl file with selected cases (for reference counts)")
    parser.add_argument("--output", required=True,
                        help="Output JSON file for domain summary")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    eval_path = Path(args.evaluation)
    output_path = Path(args.output)

    # Load data
    exec_results = load_results_json(results_dir)
    eval_results = load_evaluation(eval_path)

    # Load cases file for expected instances per domain
    cases_by_domain = defaultdict(list)
    if args.cases_file:
        cases_path = Path(args.cases_file)
        if cases_path.exists():
            with open(cases_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        tid = json.loads(line)["task_id"]
                        cases_by_domain[get_repo_from_instance_id(tid)].append(tid)

    # Join execution + evaluation by instance_id, group by domain
    all_instance_ids = set(exec_results.keys()) | set(eval_results.keys())
    domains = defaultdict(lambda: {
        "instances": [],
        "full": 0,
        "partial": 0,
        "none": 0,
        "errors": 0,
        "total_duration": 0.0,
        "duration_count": 0,
        "total_tokens": 0,
        "token_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    })

    for instance_id in sorted(all_instance_ids):
        repo = get_repo_from_instance_id(instance_id)
        d = domains[repo]
        d["instances"].append(instance_id)

        exec_entry = exec_results.get(instance_id, {})
        eval_entry = eval_results.get(instance_id, {})

        # Resolution status
        resolved = eval_entry.get("resolved", "NO")
        if resolved == "FULL":
            d["full"] += 1
        elif resolved == "PARTIAL":
            d["partial"] += 1
        else:
            d["none"] += 1
        if "error" in eval_entry:
            d["errors"] += 1

        # Execution time
        duration = exec_entry.get("duration_seconds")
        if duration is not None:
            d["total_duration"] += duration
            d["duration_count"] += 1

        # Token usage
        total_tokens = exec_entry.get("total_tokens", 0)
        if total_tokens:
            d["total_tokens"] += total_tokens
            d["token_count"] += 1
            d["total_input_tokens"] += exec_entry.get("input_tokens", 0)
            d["total_output_tokens"] += exec_entry.get("output_tokens", 0)

    # Build per-domain summary
    domain_summary = {}
    for repo in sorted(domains.keys()):
        d = domains[repo]
        total = len(d["instances"])
        full = d["full"]
        accuracy = (full / total * 100.0) if total > 0 else 0.0
        avg_duration = (d["total_duration"] / d["duration_count"]) if d["duration_count"] > 0 else 0.0
        avg_tokens = (d["total_tokens"] / d["token_count"]) if d["token_count"] > 0 else 0.0

        domain_summary[repo] = {
            "total_instances": total,
            "fully_resolved": full,
            "partially_resolved": d["partial"],
            "not_resolved": d["none"],
            "errors": d["errors"],
            "accuracy_percentage": round(accuracy, 2),
            "avg_duration_seconds": round(avg_duration, 2),
            "avg_total_tokens": round(avg_tokens, 0),
            "avg_input_tokens": round(d["total_input_tokens"] / d["token_count"], 0) if d["token_count"] > 0 else 0,
            "avg_output_tokens": round(d["total_output_tokens"] / d["token_count"], 0) if d["token_count"] > 0 else 0,
        }

    # Overall summary
    all_total = sum(d["total_instances"] for d in domain_summary.values())
    all_full = sum(d["fully_resolved"] for d in domain_summary.values())
    all_duration = sum(domains[r]["total_duration"] for r in domain_summary)
    all_duration_count = sum(domains[r]["duration_count"] for r in domain_summary)
    all_tokens = sum(domains[r]["total_tokens"] for r in domain_summary)
    all_token_count = sum(domains[r]["token_count"] for r in domain_summary)

    overall = {
        "total_instances": all_total,
        "fully_resolved": all_full,
        "accuracy_percentage": round(all_full / all_total * 100.0, 2) if all_total > 0 else 0.0,
        "avg_duration_seconds": round(all_duration / all_duration_count, 2) if all_duration_count > 0 else 0.0,
        "avg_total_tokens": round(all_tokens / all_token_count, 0) if all_token_count > 0 else 0,
    }

    output = {
        "domains": domain_summary,
        "overall": overall,
    }

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Print table
    print(f"\n{'='*90}")
    print(f"{'Domain':<20} {'Total':>6} {'Full':>6} {'Acc%':>8} {'AvgTime(s)':>12} {'AvgTokens':>12}")
    print(f"{'-'*90}")
    for repo in sorted(domain_summary.keys()):
        d = domain_summary[repo]
        print(f"{repo:<20} {d['total_instances']:>6} {d['fully_resolved']:>6} "
              f"{d['accuracy_percentage']:>7.1f}% {d['avg_duration_seconds']:>12.1f} "
              f"{d['avg_total_tokens']:>12.0f}")
    print(f"{'-'*90}")
    print(f"{'OVERALL':<20} {overall['total_instances']:>6} {overall['fully_resolved']:>6} "
          f"{overall['accuracy_percentage']:>7.1f}% {overall['avg_duration_seconds']:>12.1f} "
          f"{overall['avg_total_tokens']:>12.0f}")
    print(f"{'='*90}")
    print(f"\nDomain summary saved to: {output_path}")


if __name__ == "__main__":
    main()
