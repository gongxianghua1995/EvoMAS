#!/usr/bin/env bash
# Re-run failed cases with optimized prompt
# Usage: nohup bash scripts/run_pro_netisol_failed_rerun.sh > logs/run_pro_netisol_failed.log 2>&1 &

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

source ~/miniconda/etc/profile.d/conda.sh

# Config
BASELINE="chatdev"
DATASET="swe_bench_pro"
MODEL="openai:Deepseek-V4-Flash-0731"
JUDGE="none"
OUTPUT_ROOT="output_pro_netisol_failed_rerun"
LOG_DIR="logs/run_pro_netisol_failed_$(date +%Y%m%d_%H%M%S)"
PYTHON="/home/xhgong/miniconda/envs/evomas/bin/python"
TASK_TIMEOUT="--task-timeout 7200"

mkdir -p "$LOG_DIR"

echo "======================================================================"
echo "# SWE-bench Pro - Failed Cases Re-run (Optimized Prompt)"
echo "# Model: $MODEL"
echo "# Container: --network none (agent shell commands have no internet)"
echo "# Task timeout: 7200s/case (2 hours)"
echo "# Optimizations:"
echo "#   1. Added FAIL_TO_PASS tests to problem statement"
echo "#   2. Enhanced ACTION_FIRST_PREAMBLE (test-first analysis)"
echo "# Output: $OUTPUT_ROOT"
echo "======================================================================"

# Run each domain with failed cases
for domain in ansible flipt openlibrary webclients; do
    TASK_FILE="scripts/task_lists/pro_netisol_failed_${domain}.txt"

    if [[ ! -f "$TASK_FILE" ]]; then
        echo "Skipping $domain (no failed cases file)"
        continue
    fi

    output_dir="${OUTPUT_ROOT}/chatdev_${domain}"
    log_file="${LOG_DIR}/${domain}_failed.log"

    mkdir -p "$output_dir"

    echo ""
    echo "----------------------------------------------------------------------"
    echo "# Re-running failed cases for: $domain"
    echo "# Task file: $TASK_FILE"
    echo "# Output: $output_dir"
    echo "# Log: $log_file"
    echo "----------------------------------------------------------------------"

    $PYTHON -u main.py \
        --dataset "$DATASET" \
        --baseline "$BASELINE" \
        --task-ids-file "$TASK_FILE" \
        --llm-as-judge "$JUDGE" \
        $TASK_TIMEOUT \
        --output-dir "$output_dir" \
        2>&1 | tee "$log_file"

    echo "# Completed: $domain at $(date)"
done

echo ""
echo "======================================================================"
echo "# Failed cases re-run completed at $(date)"
echo "# Results in: $OUTPUT_ROOT/"
echo "# Next: merge patches and run Docker eval"
echo "======================================================================"
