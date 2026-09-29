# 最新 SWE 实验与数据

新实验入口：[断网基线快速启动指南](../SWE_OFFLINE_QUICKSTART.md)。

整理日期：2026-09-28。保留已有适配实现与最新断网实验；本次整理没有重新执行 benchmark。

| 数据集 | 报告成绩 | 报告 |
|---|---:|---|
| Verified test 子集 | 106/154，68.8% | [断网实验报告](offline_20260910/SWE-bench%20断网实验报告.md) |
| Pro test 子集 | 65/216，30.1% | [Pro 断网实验报告](offline_20260910/SWE-bench%20Pro%20断网实验报告.md) |

**统计口径：这是历史运行与优化重跑的合并结果。** Pro 报告明确取多次尝试最优，并从成功数中排除 15 个空补丁假阳性；分母仍为 216。Verified 也包含调整 step limit 后的重跑。本次保留原报告，不把历史结果改写为单次运行成绩。

该版本向生成阶段注入 FAIL_TO_PASS 测试名，协议与 GPTSwarm、MetaGPT 最新实验并不完全相同。原报告的联网/断网差异伴随其他改动，不能单独归因于网络。历史 token 计数覆盖和口径也不等同于完整 API usage，不宜直接横向比较。

## 已提交的材料

- `offline_20260910/attempts.jsonl`：原版与必要重跑的 563 条尝试记录，保留源文件位置、原始评分字段、可用时间/token 字段和补丁 SHA-256。
- `offline_20260910/patches/`、`evaluations/`：对应补丁与已有评估摘要。
- `offline_20260910/export_manifest.json`：源结果文件及导出文件校验值。
- `offline_20260910/report_scope.json`：报告统计口径与限制。
- `../../dataset/splits/`：数据划分和 Pro 本地输入的 SHA-256 清单。大体积 Pro 原始输入继续保留在本机 `dataset/swe_bench_pro/`，不提交；迁移机器时需另行准备并核对 `pro_local_inputs_manifest.json`。

`resolved` 等历史字段可能包含旧评估状态及空补丁假阳性；不能直接累加所有尝试的 `FULL`。导出保留原始字段，没有凭清理过程重新选择“最终补丁”。复核时须结合补丁、评估记录及原报告的筛选规则。

## 本地保留与归档

完整原始数据继续留在以下 Git 忽略目录：

- `output_pro_netisol/`
- `output_pro_netisol_failed_rerun/`
- `output_pro_netisol_timeout/`
- `output_verified_netisol/`
- `output_verified_netisol_rerun/`

相关断网日志与评估日志保留在 `logs/`；有复核歧义的混合评估目录未删除。旧 `output_paper/` 等目录移至仓库外归档；当前报告已复制到本目录。历史说明中的旧输出路径应从归档查阅。

本机归档：`/home/xhgong/project_cleanup_archive/20260928_swe_baselines/EvoMAS/`。总清单见其父目录 `manifest.json`，每项记录原相对路径、文件数、大小及原因；恢复时复制到原相对路径，避免覆盖现有文件。

## 执行入口

- 生成：`scripts/run_verified_network_isolated.sh`、`scripts/run_pro_network_isolated.sh`。
- 评估：`scripts/run_swebench_docker_eval.py`、`scripts/run_swebench_pro_eval.py`。
- 必要重跑：`scripts/run_pro_netisol_failed_rerun.sh`、`scripts/run_pro_netisol_timeout_rerun.sh`。失败任务清单已从根目录 `temp_failed_*.txt` 迁到 `scripts/task_lists/pro_netisol_failed_*.txt`，启动脚本同步更新。

脚本中的环境路径来自实验机器，迁移时按本机环境设置。API 密钥仅放本地 `.env`，不提交。
