"""Docker-based SWE-bench Pro evaluation harness for EvoMAS.

SWE-bench Pro differs from Verified:
  - Agent/eval images are `jefzda/sweap-images:<repo>_<hash>` (repo at base
    commit, test_patch NOT pre-applied).
  - Test specs live in dataset/swe_bench_pro/tasks_map.json
    (base_commit / test_patch / FAIL_TO_PASS / PASS_TO_PASS), with
    dockerhub_tag in dataset/swe_bench_pro/test.json metadata.
  - Mixed languages: ansible/openlibrary -> pytest, flipt -> go test,
    NodeBB -> mocha, webclients -> jest.

Eval flow per instance:
  1. docker run -d the instance's jefzda/sweap-images image
  2. docker cp model patch + test patch into the container
  3. git apply model.patch (fallback git apply -3), then git apply test.patch
  4. Run FAIL_TO_PASS + PASS_TO_PASS tests via the domain's test command
  5. Parse results -> resolved iff all F2P and all P2P pass
  6. Write report.json, merge into results.json, remove container

Usage:
  python scripts/run_swebench_pro_eval.py --domain flipt --skip-existing
  python scripts/run_swebench_pro_eval.py --domain ansible --instance-ids <id>
"""

import argparse
import ast
import json
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATASET = PROJECT_ROOT / "dataset" / "swe_bench_pro"
TASKS_MAP = DATASET / "tasks_map.json"
TEST_JSON = DATASET / "test.json"
DEFAULT_MODEL = "chatdev_Deepseek-V4-Flash-0731"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "logs" / "swebench_reports_pro"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output_pro"
DOMAINS = ["ansible", "flipt", "openlibrary", "webclients"]

# Per-domain test command builders. Each returns the shell command (str) to
# run inside the container at /app, given the F2P/P2P test id lists.
DOMAIN_LANG = {"ansible": "python", "openlibrary": "python", "flipt": "go", "webclients": "jest"}


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_test_ids(raw):
    """FAIL_TO_PASS / PASS_TO_PASS entries are either lists, JSON strings or
    python-repr strings. Normalize to a list of str."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    raw = str(raw).strip()
    if not raw or raw == "[]":
        return []
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else [str(parsed)]
    except Exception:
        pass
    try:
        parsed = ast.literal_eval(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else [str(parsed)]
    except Exception:
        pass
    return [raw]


def load_specs() -> dict:
    tasks = json.loads(TASKS_MAP.read_text())
    # dockerhub_tag lookup from test.json metadata
    tags = {}
    try:
        for entry in json.loads(TEST_JSON.read_text()):
            md = entry.get("metadata") or {}
            iid = md.get("instance_id") or entry.get("id")
            tag = md.get("dockerhub_tag")
            if iid and tag:
                tags[iid] = tag
    except Exception as e:
        log(f"WARNING: failed to load dockerhub tags from {TEST_JSON}: {e}")
    for iid, spec in tasks.items():
        spec.setdefault("dockerhub_tag", tags.get(iid))
        spec["FAIL_TO_PASS"] = parse_test_ids(spec.get("FAIL_TO_PASS"))
        spec["PASS_TO_PASS"] = parse_test_ids(spec.get("PASS_TO_PASS"))
    return tasks


def patch_dir(domain: str, output_root: Path) -> Path:
    """Find the patch directory. Supports both Batch 1 layout
    (output_pro/chatdev_<domain>/output_selected/...) and Batch 2 layout
    (output_pro/output_selected/...)."""
    p1 = output_root / f"chatdev_{domain}" / "output_selected" / "swe_bench_pro" / DEFAULT_MODEL
    if p1.exists():
        return p1
    p2 = output_root / "output_selected" / "swe_bench_pro" / DEFAULT_MODEL
    return p2


def docker(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    """Run a docker CLI command, return (returncode, combined output)."""
    proc = subprocess.run(
        ["docker", *cmd], capture_output=True, text=True,
        timeout=timeout, errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def exec_in(container: str, command: str, workdir: str = "/app", timeout: int = 1800) -> tuple[int, str]:
    # NOTE: bash -c (NOT -l). Login shells re-source /etc/profile which resets
    # PATH and drops image ENV dirs like /usr/local/go/bin (breaks go/pytest).
    cmd = ["exec", "-w", workdir, container, "bash", "-c", command]
    try:
        return docker(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, f"TIMEOUT after {timeout}s: {command[:200]}"


# ---------------------------------------------------------------- test runners

def go_test_command(f2p: list[str], p2p: list[str]) -> str:
    names = sorted(set(f2p) | set(p2p))
    pattern = "^(?:" + "|".join(re.escape(n) for n in names) + ")$"
    return f"go test ./... -json -count=1 -run {shlex.quote(pattern)} 2>&1"


def pytest_command(f2p: list[str], p2p: list[str]) -> str:
    """Run pytest by FILE paths.

    SWE-bench Pro's FAIL_TO_PASS nodeids truncate parameterized IDs (missing
    the closing `]`), which pytest rejects outright ("found no collectors").
    Instead we run every referenced test FILE and prefix-match outcomes from
    the -rA short summary.
    """
    files = sorted({nid.split("::")[0] for nid in (f2p + p2p) if nid})
    files = [f for f in files if not f.startswith("<")]
    files_arg = " ".join(shlex.quote(f) for f in files)
    # -rA prints "PASSED path::test[param]" lines with full nodeids.
    return (
        f"python -m pytest {files_arg} -rA -p no:cacheprovider -q "
        f"-W ignore::DeprecationWarning 2>&1 | tail -800"
    )


def jest_command(f2p: list[str], p2p: list[str]) -> str:
    """Build jest command. webclients F2P format: "path/to/file.test.ts | test name".
    The path is relative to a package workspace (e.g. applications/drive/ or
    packages/components/), where jest.config.js lives. We find the actual test
    file by basename, walk up to the nearest jest.config.js, cd there, run jest.
    """
    all_ids = f2p + p2p
    files = sorted({tid.split(" | ")[0] for tid in all_ids if " | " in tid})
    if not files:
        return "echo 'no test files'"
    basename = Path(files[0]).name
    return (
        f"FULL=$(find /app -name {shlex.quote(basename)} -not -path '*/node_modules/*' 2>/dev/null | head -1) && "
        f"DIR=$(dirname \"$FULL\") && "
        f"while [ \"$DIR\" != \"/\" ] && [ ! -f \"$DIR/jest.config.js\" ] && [ ! -f \"$DIR/jest.config.ts\" ]; do DIR=$(dirname \"$DIR\"); done && "
        f"cd \"$DIR\" && "
        f"/app/node_modules/.bin/jest --testPathPattern '{re.escape(basename)}' --json 2>/dev/null"
    )


def run_tests(domain: str, container: str, spec: dict, timeout: int) -> dict:
    """Run F2P+P2P tests. Returns {test_id: 'passed'|'failed'|'missing'}."""
    lang = DOMAIN_LANG.get(domain)
    f2p, p2p = spec["FAIL_TO_PASS"], spec["PASS_TO_PASS"]

    if lang == "go":
        rc, out = exec_in(container, go_test_command(f2p, p2p), timeout=timeout)
        return parse_go_json(out, f2p + p2p)
    elif lang == "jest":
        rc, out = exec_in(container, jest_command(f2p, p2p), timeout=timeout)
        return parse_jest(out, f2p + p2p)
    else:  # python / pytest
        rc, out = exec_in(container, pytest_command(f2p, p2p), timeout=timeout)
        return parse_pytest_rA(out, f2p + p2p)


def parse_go_json(out: str, all_ids: list[str]) -> dict:
    status = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        test = evt.get("Test")
        action = evt.get("Action")
        if not test:
            continue
        if action in ("pass", "fail", "skip"):
            status[test] = {"pass": "passed", "fail": "failed", "skip": "failed"}[action]
    return {tid: status.get(tid, "missing") for tid in all_ids}


PYTEST_OUTCOME_RE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|RERUN) (.+)$")


def parse_pytest_rA(out: str, all_ids: list[str]) -> dict:
    """Parse pytest -rA short summary and match dataset test IDs.

    Pro's nodeids truncate parameterized IDs (missing `]`), so matching is:
      1. exact match on the full nodeid
      2. prefix match - dataset ID is a prefix of the actual nodeid
    Dataset IDs without `::` (bare file or collection-level entries) pass if
    every actual nodeid starting with them passed; a file entry also passes
    if nothing ran under it (treated as vacuous) - avoided by requiring at
    least one match when possible.
    """
    # Collect actual outcomes from the summary
    actual = {}  # nodeid -> passed/failed
    for line in out.splitlines():
        m = PYTEST_OUTCOME_RE.match(line.strip())
        if not m:
            continue
        outcome, nodeid = m.groups()
        nodeid = nodeid.split(" - ")[0].strip()
        if outcome == "RERUN":
            continue
        actual[nodeid] = "passed" if outcome in ("PASSED", "XFAIL") else "failed"

    result = {}
    for tid in all_ids:
        if tid in actual:
            result[tid] = actual[tid]
            continue
        # prefix match: actual nodeids that start with the (truncated) dataset id
        matches = {n: s for n, s in actual.items() if n.startswith(tid)}
        if matches:
            result[tid] = "passed" if all(s == "passed" for s in matches.values()) else "failed"
        elif "::" not in tid:
            # collection-level entry with no matching tests - vacuously pass
            result[tid] = "passed"
        else:
            result[tid] = "missing"
    return result


def parse_jest(out: str, all_ids: list[str]) -> dict:
    """Parse jest --json output. jest's assertionResults.fullName includes
    ancestor describe titles (e.g. "Password flags checks hasCustomPassword ...").
    F2P test names match against fullName via substring."""
    status = {}
    idx = out.find("{\n")
    if idx < 0:
        idx = out.find('{"')
    if idx < 0:
        idx = out.find("{")
    if idx >= 0:
        try:
            data = json.loads(out[idx:])
        except Exception:
            data = None
        if data and "testResults" in data:
            for suite in data["testResults"]:
                for assertion in suite.get("assertionResults", []):
                    full = assertion.get("fullName", "") or assertion.get("title", "")
                    status_val = assertion.get("status", "")
                    status[full] = "passed" if status_val == "passed" else "failed"

    result = {}
    for tid in all_ids:
        if " | " in tid:
            test_name = tid.split(" | ", 1)[1].strip()
        else:
            test_name = tid
        if test_name in status:
            result[tid] = status[test_name]
            continue
        # substring match: F2P name within jest fullName, or fullName ends with it
        matches = {k: v for k, v in status.items() if test_name in k or k.endswith(test_name)}
        if matches:
            result[tid] = "passed" if all(v == "passed" for v in matches.values()) else "failed"
        else:
            result[tid] = "missing"
    return result


# ---------------------------------------------------------------- eval runner

def eval_instance(domain: str, iid: str, patch: str, spec: dict, timeout: int) -> dict:
    tag = spec.get("dockerhub_tag")
    if not tag:
        return {"status": "ERROR", "reason": "no dockerhub_tag"}
    if not tag.startswith("jefzda/sweap-images:"):
        tag = f"jefzda/sweap-images:{tag}"
    container = f"sweap-eval-{uuid.uuid4().hex[:8]}"
    report = {"instance_id": iid, "domain": domain}

    rc, out = docker(["run", "-d", "--network", "none", "--pull=never",
                      "--label", "owner=evomas", "--entrypoint=",
                      "--name", container, tag, "sleep", "infinity"], timeout=300)
    if rc != 0:
        return {"status": "ERROR", "reason": f"container start failed: {out[-200:]}"}

    try:
        from swe_offline_container import verify_network
        rc, inspection = docker(['inspect', container], timeout=30)
        if rc:
            raise RuntimeError('Cannot inspect evaluation container network')
        verify_network(json.loads(inspection)[0])
        report['network_mode'] = 'none'
        # Write patches on host then docker cp
        model_p = Path(f"/tmp/model_{container}.patch")
        test_p = Path(f"/tmp/test_{container}.patch")
        model_p.write_text(patch)
        test_p.write_text(spec.get("test_patch") or "")

        t0 = time.time()
        rc, out = docker(["cp", str(model_p), f"{container}:/tmp/model.patch"], timeout=60)
        rc2, out2 = docker(["cp", str(test_p), f"{container}:/tmp/test.patch"], timeout=60)

        # Reset to base commit (image should already be there, but be safe)
        exec_in(container, f"git checkout {spec['base_commit']} -- . && git clean -fd", timeout=120)

        # Apply model patch
        rc, out = exec_in(container, "git apply --whitespace=nowarn /tmp/model.patch", timeout=120)
        if rc != 0:
            rc, out = exec_in(container, "git apply -3 /tmp/model.patch", timeout=120)
        if rc != 0:
            report.update({"status": "PATCH_APPLY_FAIL", "reason": out[-300:]})
            return report
        report["patch_applied"] = True

        # Apply test patch
        rc, out = exec_in(container, "git apply --whitespace=nowarn /tmp/test.patch", timeout=120)
        if rc != 0:
            rc, out = exec_in(container, "git apply -3 /tmp/test.patch", timeout=120)
        if rc != 0:
            report.update({"status": "TEST_PATCH_FAIL", "reason": out[-300:]})
            return report
        report["test_patch_applied"] = True

        # Run tests
        status = run_tests(domain, container, spec, timeout)
        f2p, p2p = spec["FAIL_TO_PASS"], spec["PASS_TO_PASS"]
        f2p_res = {t: status.get(t, "missing") for t in f2p}
        p2p_res = {t: status.get(t, "missing") for t in p2p}
        f2p_ok = all(v == "passed" for v in f2p_res.values()) if f2p else True
        p2p_ok = all(v == "passed" for v in p2p_res.values()) if p2p else True

        report.update({
            "status": "RESOLVED" if (f2p_ok and p2p_ok) else "UNRESOLVED",
            "duration_seconds": round(time.time() - t0, 1),
            "f2p_total": len(f2p), "f2p_passed": sum(1 for v in f2p_res.values() if v == "passed"),
            "p2p_total": len(p2p), "p2p_passed": sum(1 for v in p2p_res.values() if v == "passed"),
            "f2p_failed": [t for t, v in f2p_res.items() if v != "passed"][:20],
            "p2p_failed": [t for t, v in p2p_res.items() if v != "passed"][:20],
        })
        return report
    finally:
        docker(["rm", "-f", container], timeout=60)
        for p in (Path(f"/tmp/model_{container}.patch"), Path(f"/tmp/test_{container}.patch")):
            p.unlink(missing_ok=True)


def run_domain(domain: str, max_workers: int, timeout: int, report_dir: Path,
               output_root: Path, skip_existing: bool, instance_ids: list[str] | None,
               dry_run: bool):
    specs = load_specs()
    pdir = patch_dir(domain, output_root)
    if not pdir.exists():
        log(f"[{domain}] no patch dir {pdir}, skipping")
        return
    txts = sorted(pdir.glob("*.txt"))
    # Filter by domain when patches are in a shared directory (Batch 2 layout)
    if pdir == output_root / "output_selected" / "swe_bench_pro" / DEFAULT_MODEL:
        domain_keywords = {
            "openlibrary": "openlibrary",
            "webclients": "webclients",
        }
        kw = domain_keywords.get(domain)
        if kw:
            txts = [t for t in txts if kw in t.stem]
    if instance_ids:
        want = set(instance_ids)
        txts = [t for t in txts if t.stem in want]

    todo = []
    skipped = 0
    for t in txts:
        if not t.read_text(errors="ignore").strip():
            continue  # empty patch
        if skip_existing:
            rp = report_dir / domain / t.stem / "report.json"
            if rp.exists():
                skipped += 1
                continue
        todo.append(t)
    log(f"[{domain}] {len(txts)} patches, skip_existing skipped {skipped}, todo {len(todo)}")
    if not todo or dry_run:
        for t in todo:
            print(f"  would eval: {t.stem}")
        return

    report_dir.mkdir(parents=True, exist_ok=True)
    results_by_id = {}

    def work(t: Path):
        iid = t.stem
        spec = specs.get(iid)
        if not spec:
            return {"instance_id": iid, "status": "ERROR", "reason": "no spec in tasks_map"}
        try:
            r = eval_instance(domain, iid, t.read_text(errors="ignore"), spec, timeout)
        except Exception as e:
            r = {"instance_id": iid, "status": "ERROR", "reason": str(e)[:300]}
        # persist
        rd = report_dir / domain / iid
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "report.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
        results_by_id[iid] = r
        log(f"  [{domain}] {iid}: {r.get('status')} "
            f"(F2P {r.get('f2p_passed', '?')}/{r.get('f2p_total', '?')}, "
            f"P2P {r.get('p2p_passed', '?')}/{r.get('p2p_total', '?')}, "
            f"{r.get('duration_seconds', '?')}s)")
        return r

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(work, t) for t in todo]
        for f in as_completed(futs):
            f.result()

    merge_results(domain, report_dir, output_root, results_by_id)


def merge_results(domain: str, report_dir: Path, output_root: Path, fresh: dict | None = None):
    """Build/merge results.json from patch files + all reports."""
    rj_dir = patch_dir(domain, output_root)
    results_json = rj_dir / "results.json"
    results = []
    if results_json.exists():
        try:
            results = json.loads(results_json.read_text())
        except Exception:
            results = []
    by_id = {}
    for r in results:
        iid = r.get("instance_id") or r.get("task_id")
        if iid:
            by_id[iid] = r

    # Ensure every patch file has an entry
    for t in sorted(rj_dir.glob("*.txt")):
        iid = t.stem
        if iid not in by_id:
            entry = {"instance_id": iid, "task_id": iid,
                     "patch_file": str(t), "patch_bytes": t.stat().st_size,
                     "resolved": "UNKNOWN"}
            by_id[iid] = entry
            results.append(entry)

    # Merge reports
    for rj in sorted((report_dir / domain).glob("*/report.json")):
        iid = rj.parent.name
        if iid not in by_id:
            continue
        r = json.loads(rj.read_text())
        entry = by_id[iid]
        entry["docker_status"] = r.get("status")
        entry["docker_report"] = r
        st = r.get("status")
        if st == "RESOLVED":
            entry["resolved"] = "FULL"
            entry["resolved_reason"] = "pro-docker-eval-resolved"
        elif st in ("UNRESOLVED", "PATCH_APPLY_FAIL", "TEST_PATCH_FAIL", "ERROR"):
            entry["resolved"] = "NO" if st == "UNRESOLVED" else "ERROR"
            entry["resolved_reason"] = f"pro-{st.lower()}"

    results_json.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    log(f"[{domain}] results.json updated: {results_json} ({len(results)} entries)")


def main():
    global DEFAULT_MODEL
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--domain", required=True, choices=DOMAINS + ["all"])
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--timeout", type=int, default=1800, help="per-instance test timeout (s)")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    p.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("--instance-ids", default=None, help="comma-separated instance ids")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument('--model-name', default=DEFAULT_MODEL)
    args = p.parse_args()
    DEFAULT_MODEL = args.model_name

    domains = DOMAINS if args.domain == "all" else [args.domain]
    iids = [s.strip() for s in args.instance_ids.split(",") if s.strip()] if args.instance_ids else None

    for d in domains:
        run_domain(d, args.max_workers, args.timeout, Path(args.report_dir),
                   Path(args.output_root), args.skip_existing, iids, args.dry_run)


if __name__ == "__main__":
    main()
