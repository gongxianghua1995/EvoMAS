"""Docker-based SWE-bench evaluation harness for EvoMAS.

This script replaces the local conda-based evaluator with the official
`swebench` package's docker harness, which uses prebuilt per-instance
images (swebench/sweb.eval.x86_64.<repo>_<repo>-<issue>:latest).

Flow
----
For each domain (django, sympy, matplotlib, scikit-learn):
  1. Scan output/chatdev_<domain>/output_selected/swe_bench_verified/
     chatdev_Deepseek-V4-Flash-0731/*.txt for patch files.
  2. Build a predictions list:
       [{"instance_id": ..., "model_name_or_path": MODEL_NAME,
         "model_patch": <patch content>}]
  3. Write predictions to /tmp/preds_<domain>_<timestamp>.json.
  4. Call swebench.run_evaluation(
         dataset_name="SWE-bench/SWE-bench_Verified", split="test",
         instance_ids=[...], predictions_path=..., run_id="...",
         max_workers=N, timeout=900, report_dir=REPORT_DIR).
  5. For each instance, read
     <REPORT_DIR>/<run_id>/<MODEL_NAME>/<instance_id>/report.json
     and extract {resolved, patch_successfully_applied, tests_status}.
  6. Merge into output/.../results.json under each entry's
     "docker_resolved" / "docker_patch_applied" / "docker_tests_status"
     fields (NEW fields; existing local results preserved).

Usage
-----
Foreground:
  python scripts/run_swebench_docker_eval.py --domain sympy

Background:
  nohup python scripts/run_swebench_docker_eval.py --domain sympy \
      --max-workers 4 > logs/swebench_docker_sympy.log 2>&1 &

All four domains in parallel:
  for d in django sympy matplotlib scikit-learn; do
    nohup python scripts/run_swebench_docker_eval.py --domain $d \
        --max-workers 4 > logs/swebench_docker_$d.log 2>&1 &
  done

CLI:
  --domain         one of django|sympy|matplotlib|scikit-learn|all
  --max-workers    parallel docker containers per domain (default 4)
  --timeout        per-instance timeout in seconds (default 900 = 15 min)
  --report-dir     root for swebench reports
                   (default logs/swebench_reports)
  --model-name     model name used as subdirectory (default
                   chatdev_Deepseek-V4-Flash-0731)
  --dry-run        build predictions.json but skip run_evaluation
  --skip-existing  skip instances with existing report.json (resume mode)
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# swebench imports - lazily loaded inside main so --help works without
# the package or docker daemon being available
DATASET_NAME = "SWE-bench/SWE-bench_Verified"
SPLIT = "test"
DEFAULT_MODEL = "chatdev_Deepseek-V4-Flash-0731"
RUN_ID_PREFIX = 'evomas_docker'
DEFAULT_REPORT_DIR = str(Path(__file__).resolve().parent.parent / "logs" / "swebench_reports")
DOMAINS = ["django", "sympy", "matplotlib", "scikit-learn", "sphinx"]


def domain_output_dir(domain: str, output_root: str | None = None) -> Path:
    """Resolve the patch directory.

    When ``output_root`` is given we auto-detect between the two common
    dataset-name variants (``swebench_verified`` vs ``swe_bench_verified``),
    because MasRunner always uses the dataset_name as the intermediate
    subdirectory, and CLI sometimes passes ``swebench_verified``, sometimes
    ``swe_bench_verified``. If neither exists yet we default to
    ``swe_bench_verified`` (the dataset's true name).
    """
    if output_root:
        base = Path(output_root) / f"chatdev_{domain}" / "output_selected"
        cand1 = base / "swe_bench_verified" / DEFAULT_MODEL
        cand2 = base / "swebench_verified" / DEFAULT_MODEL
        if cand1.exists():
            return cand1
        if cand2.exists():
            return cand2
        # Fallback: prefer true dataset name
        return cand1
    return (
        PROJECT_ROOT / "output" / f"chatdev_{domain}"
        / "output_selected" / "swe_bench_verified" / DEFAULT_MODEL
    )


def load_predictions(
    domain: str,
    output_root: str | None = None,
    instance_ids_filter: list[str] | None = None,
) -> list:
    """Build swebench-format predictions from per-instance .txt patch files."""
    out_dir = domain_output_dir(domain, output_root)
    predictions = []
    if not out_dir.exists():
        print(f"[{domain}] output dir not found: {out_dir}")
        return predictions

    if instance_ids_filter:
        filter_set = set(instance_ids_filter)
        txt_files = []
        for iid in instance_ids_filter:
            p = out_dir / f"{iid}.txt"
            if p.exists():
                txt_files.append(p)
            else:
                print(f"  [{domain}] {iid}: NOT FOUND in {out_dir}")
    else:
        txt_files = sorted(out_dir.glob("*.txt"))
        filter_set = None
    print(f"[{domain}] found {len(txt_files)} .txt files under {out_dir}")
    for txt in txt_files:
        instance_id = txt.stem
        if filter_set and instance_id not in filter_set:
            continue
        patch = txt.read_text(errors="ignore")
        if not patch.strip():
            print(f"  [{domain}] {instance_id}: empty patch")
        predictions.append({
            "instance_id": instance_id,
            "model_name_or_path": DEFAULT_MODEL,
            "model_patch": patch,
        })
    return predictions


def write_predictions(predictions: list, domain: str) -> Path:
    out = Path(f"/tmp/preds_{domain}_{int(time.time())}.json")
    out.write_text(json.dumps(predictions, indent=2))
    print(f"[{domain}] wrote {len(predictions)} predictions to {out}")
    return out


def run_domain(
    domain: str,
    max_workers: int,
    timeout: int,
    report_dir: str,
    model_name: str,
    dry_run: bool,
    skip_existing: bool,
    output_root: str | None = None,
    instance_ids: list[str] | None = None,
):
    print(f"\n{'='*70}")
    print(f"=== Domain: {domain}  workers={max_workers}  timeout={timeout}s")
    if instance_ids:
        print(f"=== Filter instances: {instance_ids}")
    if output_root:
        print(f"=== Output root override: {output_root}")
    print(f"{'='*70}")

    predictions = load_predictions(domain, output_root, instance_ids)
    if not predictions:
        print(f"[{domain}] no predictions, skipping")
        return

    preds_path = write_predictions(predictions, domain)
    if dry_run:
        print(f"[{domain}] dry-run: not calling run_evaluation")
        return

    import swebench
    import swebench.harness.run_evaluation as harness
    from scripts.swe_offline_container import create_offline_container
    from swebench.harness.utils import load_swebench_dataset

    instance_ids = [p["instance_id"] for p in predictions]

    if skip_existing:
        # Filter out instances that already have a report.json
        run_id = f"{RUN_ID_PREFIX}_{domain}"
        kept = []
        skipped = 0
        for iid in instance_ids:
            report = (
                Path(report_dir) / run_id
                / model_name.replace("/", "__") / iid / "report.json"
            )
            if report.exists():
                skipped += 1
            else:
                kept.append(iid)
        print(f"[{domain}] skip_existing: {skipped} done, {len(kept)} todo")
        instance_ids = kept
        if not instance_ids:
            print(f"[{domain}] nothing to do")
            return

    run_id = f"{RUN_ID_PREFIX}_{domain}"
    print(f"[{domain}] run_id={run_id}  instances={len(instance_ids)}")
    print(f"[{domain}] calling swebench.run_evaluation...")

    original_create = harness.create_container
    harness.create_container = create_offline_container
    try:
        summary = swebench.run_evaluation(
            dataset_name=DATASET_NAME,
            split=SPLIT,
            instance_ids=instance_ids,
            predictions_path=str(preds_path),
            max_workers=max_workers,
            open_file_limit=4096,
            run_id=run_id,
            timeout=timeout,
            rewrite_reports=False,
            modal=False,
            report_dir=report_dir,
            task_repo=None,
        )
        print(f"[{domain}] run_evaluation returned summary with keys: "
              f"{list(summary.keys()) if isinstance(summary, dict) else 'N/A'}")
        if isinstance(summary, dict):
            for k, v in summary.items():
                if k != "resolved_instances":
                    print(f"  {k}: {v}")
    except Exception as e:
        print(f"[{domain}] run_evaluation EXCEPTION: {e}")
        traceback.print_exc()
        raise
    finally:
        harness.create_container = original_create

    merge_results_back(domain, run_id, report_dir, model_name, output_root)


def merge_results_back(
    domain: str, run_id: str, report_dir: str, model_name: str,
    output_root: str | None = None,
):
    """Read the run-level summary report and merge docker_resolved /
    docker_patch_applied / docker_tests_status fields back into our
    results.json (without overwriting existing local-eval fields).

    swebench 5.0.2 writes the summary to:
        <report_dir>/<model_name>.<run_id>.json
    NOT to per-instance subdirectories — the per-instance reports
    (run_instance.log, test_output.txt, patch.diff, report.json) live
    under logs/run_evaluation/<run_id>/<model_name>/<instance_id>/
    which is harder to discover. The summary file alone has
    resolved_ids / unresolved_ids / empty_patch_ids / error_ids lists
    which is sufficient for our merge.
    """
    out_dir = domain_output_dir(domain, output_root)
    results_json = out_dir / "results.json"
    if not results_json.exists():
        print(f"[{domain}] results.json not found at {results_json}, "
              f"not merging back")
        return

    # swebench 5.0.2 layout: <report_dir>/<model>.<run_id>.json
    summary_path = (
        Path(report_dir) / f"{model_name.replace('/', '__')}.{run_id}.json"
    )
    if not summary_path.exists():
        print(f"[{domain}] summary report not found: {summary_path}")
        return
    print(f"[{domain}] reading summary from {summary_path}")
    summary = json.loads(summary_path.read_text())
    resolved_ids = set(summary.get("resolved_ids", []))
    unresolved_ids = set(summary.get("unresolved_ids", []))
    empty_patch_ids = set(summary.get("empty_patch_ids", []))
    error_ids = set(summary.get("error_ids", []))
    infra_failure_ids = set(summary.get("infra_failure_ids", []))
    print(f"[{domain}] summary: {len(resolved_ids)} resolved, "
          f"{len(unresolved_ids)} unresolved, {len(empty_patch_ids)} empty, "
          f"{len(error_ids)} errors")

    # Load our results.json
    try:
        results = json.loads(results_json.read_text())
    except Exception as e:
        print(f"[{domain}] failed to read {results_json}: {e}")
        return

    # Build instance_id -> result entry map (results.json uses
    # task_id / instance_id interchangeably)
    by_id = {}
    for r in results:
        iid = r.get("instance_id") or r.get("task_id")
        if iid:
            by_id[iid] = r

    updated = 0
    for iid in (resolved_ids | unresolved_ids | empty_patch_ids
                | error_ids | infra_failure_ids):
        r = by_id.get(iid)
        if not r:
            continue
        if iid in resolved_ids:
            r["docker_resolved"] = True
            r["docker_patch_applied"] = True
            r["docker_infra_failure"] = False
            r["docker_status"] = "RESOLVED"
            old = r.get("resolved", "NO")
            if old in ("NO", "UNKNOWN", None, "", "ERROR"):
                r["resolved"] = "FULL"
                r["resolved_reason"] = "docker-resolved"
            else:
                r["resolved_reason"] = f"docker-resolved (local was: {old})"
        elif iid in unresolved_ids:
            r["docker_resolved"] = False
            r["docker_patch_applied"] = True
            r["docker_infra_failure"] = False
            r["docker_status"] = "UNRESOLVED"
            if r.get("resolved") in ("NO", "UNKNOWN", None, "", "ERROR"):
                r["resolved"] = "NO"
                r["resolved_reason"] = "docker-applied-but-tests-failed"
        elif iid in empty_patch_ids:
            r["docker_resolved"] = False
            r["docker_patch_applied"] = False
            r["docker_status"] = "EMPTY_PATCH"
            if r.get("resolved") in ("UNKNOWN", None, ""):
                r["resolved"] = "NO"
                r["resolved_reason"] = "docker-empty-patch"
        elif iid in error_ids:
            r["docker_resolved"] = False
            r["docker_patch_applied"] = False
            r["docker_status"] = "ERROR"
            if r.get("resolved") in ("UNKNOWN", None, ""):
                r["resolved"] = "NO"
                r["resolved_reason"] = "docker-eval-error"
        elif iid in infra_failure_ids:
            r["docker_resolved"] = False
            r["docker_patch_applied"] = False
            r["docker_infra_failure"] = True
            r["docker_status"] = "INFRA_FAILURE"
        updated += 1

    # Write back
    results_json.write_text(json.dumps(results, indent=2))
    print(f"[{domain}] merged docker results into {updated}/"
          f"{len(results)} entries of {results_json}")

    # Print summary table
    print(f"\n[{domain}] SUMMARY:")
    print(f"  {'instance_id':50s}  status              local→docker")
    all_iids = sorted(
        resolved_ids | unresolved_ids | empty_patch_ids | error_ids
        | infra_failure_ids
    )
    for iid in all_iids:
        r = by_id.get(iid, {})
        if iid in resolved_ids:
            st = "RESOLVED"
        elif iid in unresolved_ids:
            st = "UNRESOLVED"
        elif iid in empty_patch_ids:
            st = "EMPTY_PATCH"
        elif iid in error_ids:
            st = "ERROR"
        else:
            st = "INFRA_FAILURE"
        local = r.get("resolved", "?")
        print(f"  {iid:50s}  {st:18s}  {local}→"
              f"{'FULL' if st == 'RESOLVED' else 'NO'}")


def main():
    global DEFAULT_MODEL, RUN_ID_PREFIX, DATASET_NAME
    p = argparse.ArgumentParser(
        description="Run SWE-bench docker-based eval for EvoMAS ChatDev "
                    "baseline results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--domain", required=True,
                   choices=DOMAINS + ["all"],
                   help="Which domain to evaluate")
    p.add_argument("--max-workers", type=int, default=4,
                   help="Parallel docker containers per domain")
    p.add_argument("--timeout", type=int, default=900,
                   help="Per-instance timeout in seconds")
    p.add_argument("--report-dir", default=DEFAULT_REPORT_DIR,
                   help="Root directory for swebench reports")
    p.add_argument("--model-name", default=DEFAULT_MODEL,
                   help="Model name (used as subdirectory in report path)")
    p.add_argument("--dry-run", action="store_true",
                   help="Build predictions.json but skip run_evaluation")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip instances that already have report.json "
                        "(resume mode)")
    p.add_argument("--merge-only", action="store_true",
                   help="Skip run_evaluation entirely; just read existing "
                        "run-level summary and merge into results.json "
                        "(use after a successful run, or to re-merge after "
                        "editing the merge function)")
    p.add_argument("--output-root", default=None,
                   help="Override output root directory. If given, looks for "
                        "<output-root>/output_selected/swebench_verified/"
                        "<model-name>/*.txt instead of the default "
                        "output/chatdev_<domain>/... path.")
    p.add_argument("--instance-ids", default=None,
                   help="Comma-separated list of instance_ids to evaluate; "
                        "if set, only runs these instances, loads their .txt "
                        "files explicitly.")
    p.add_argument('--run-id-prefix', default=RUN_ID_PREFIX,
                   help='Unique experiment prefix; avoids reusing old harness results')
    p.add_argument('--dataset-path', default=DATASET_NAME,
                   help='Prepared official Verified JSON/JSONL or cached dataset name')
    args = p.parse_args()
    DEFAULT_MODEL = args.model_name
    RUN_ID_PREFIX = args.run_id_prefix
    DATASET_NAME = args.dataset_path

    os.makedirs(args.report_dir, exist_ok=True)

    if args.merge_only:
        for d in (DOMAINS if args.domain == "all" else [args.domain]):
            run_id = f"{RUN_ID_PREFIX}_{d}"
            merge_results_back(d, run_id, args.report_dir, args.model_name,
                               args.output_root)
        return

    iids = (
        [s.strip() for s in args.instance_ids.split(",") if s.strip()]
        if args.instance_ids else None
    )

    if args.domain == "all":
        for d in DOMAINS:
            run_domain(
                domain=d,
                max_workers=args.max_workers,
                timeout=args.timeout,
                report_dir=args.report_dir,
                model_name=args.model_name,
                dry_run=args.dry_run,
                skip_existing=args.skip_existing,
                output_root=args.output_root,
                instance_ids=iids,
            )
    else:
        run_domain(
            domain=args.domain,
            max_workers=args.max_workers,
            timeout=args.timeout,
            report_dir=args.report_dir,
            model_name=args.model_name,
            dry_run=args.dry_run,
            skip_existing=args.skip_existing,
            output_root=args.output_root,
            instance_ids=iids,
        )


if __name__ == "__main__":
    main()
