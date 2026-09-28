#!/usr/bin/env bash
# Re-run timeout cases with extended timeout (14400s = 4 hours)
# Usage: nohup bash scripts/run_pro_netisol_timeout_rerun.sh > logs/run_pro_netisol_timeout.log 2>&1 &

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
OUTPUT_ROOT="output_pro_netisol_timeout"
LOG_DIR="logs/run_pro_netisol_timeout_$(date +%Y%m%d_%H%M%S)"
PYTHON="/home/xhgong/miniconda/envs/evomas/bin/python"
TASK_TIMEOUT="--task-timeout 14400"

mkdir -p "$LOG_DIR"

# Timeout cases to re-run (from previous run)
TIMEOUT_CASES=(
    "instance_protonmail__webclients-09fcf0dbdb87fa4f4a27700800ee4a3caed8b413"
    "instance_protonmail__webclients-2dce79ea4451ad88d6bfe94da22e7f2f988efa60"
    "instance_flipt-io__flipt-0fd09def402258834b9d6c0eaa6d3b4ab93b4446"
)

echo "======================================================================"
echo "# SWE-bench Pro - Timeout Cases Re-run (4 hours timeout)"
echo "# Model: $MODEL"
echo "# Container: --network none (agent shell commands have no internet)"
echo "# Task timeout: 14400s/case (4 hours)"
echo "# Cases to re-run: ${#TIMEOUT_CASES[@]}"
echo "# Output: $OUTPUT_ROOT"
echo "======================================================================"

# Create temp file with task IDs
TEMP_FILE=$(mktemp)
for case_id in "${TIMEOUT_CASES[@]}"; do
    echo "$case_id" >> "$TEMP_FILE"
done

echo "Timeout cases:"
cat "$TEMP_FILE"
echo ""

# Determine domain from instance ID
get_domain() {
    local instance_id=$1
    if [[ "$instance_id" == *"ansible"* ]]; then
        echo "ansible"
    elif [[ "$instance_id" == *"flipt"* ]]; then
        echo "flipt"
    elif [[ "$instance_id" == *"openlibrary"* ]]; then
        echo "openlibrary"
    elif [[ "$instance_id" == *"webclients"* ]]; then
        echo "webclients"
    fi
}

# Group by domain
declare -A DOMAIN_CASES
for case_id in "${TIMEOUT_CASES[@]}"; do
    domain=$(get_domain "$case_id")
    DOMAIN_CASES["$domain"]+="$case_id"$'\n'
done

# Run each domain
for domain in "${!DOMAIN_CASES[@]}"; do
    output_dir="${OUTPUT_ROOT}/chatdev_${domain}"
    log_file="${LOG_DIR}/${domain}_timeout.log"

    mkdir -p "$output_dir"

    echo ""
    echo "----------------------------------------------------------------------"
    echo "# Re-running timeout cases for: $domain"
    echo "# Output: $output_dir"
    echo "# Log: $log_file"
    echo "----------------------------------------------------------------------"

    # Create domain-specific task file
    DOMAIN_TEMP=$(mktemp)
    echo -n "${DOMAIN_CASES[$domain]}" > "$DOMAIN_TEMP"

    $PYTHON -u main.py \
        --dataset "$DATASET" \
        --baseline "$BASELINE" \
        --task-ids-file "$DOMAIN_TEMP" \
        --llm-as-judge "$JUDGE" \
        $TASK_TIMEOUT \
        --output-dir "$output_dir" \
        2>&1 | tee "$log_file"

    rm -f "$DOMAIN_TEMP"

    echo "# Completed: $domain at $(date)"
done

rm -f "$TEMP_FILE"

echo ""
echo "======================================================================"
echo "# Timeout re-run completed at $(date)"
echo "# Results in: $OUTPUT_ROOT/"
echo "# Next: merge patches and run Docker eval"
echo "======================================================================"
