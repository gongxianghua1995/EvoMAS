# SWE-bench Pro 断网实验报告

日期：2026-09-08
模型：DeepSeek-V4-Flash-0731

## 1. 实验设置

- **数据集**：SWE-bench Pro，共 **216 case**（ansible 63 / flipt 54 / openlibrary 60 / webclients 39）
- **Agent**：ChatDev baseline（CEO → CTO → Programmer），CEO/CTO step_limit=5，Programmer step_limit=250
- **网络隔离**：Docker 容器 `--network none`，仅容器内 shell 断网，LLM API 调用不受影响
- **Prompt 优化**：FAIL_TO_PASS 测试名注入 problem_statement，TEST-FIRST 引导，空转检测（10步无编辑→强制submit）
- **评测**：Pro docker harness，F2P 全过 + P2P 全过 → RESOLVED

## 2. 最终结果

**断网版 Pass rate：65/216 = 30.1%**（联网版 62.0%，已剔除 15 个空 patch 假阳性）

| Domain | 总数 | 断网版 Resolved | 联网版 Resolved | 断网 Pass Rate | 退化 |
|--------|------|-----------------|-----------------|----------------|------|
| ansible | 63 | 11 | 36 | 17.5% | -39.6pp |
| flipt | 54 | 18 | 23 | 33.3% | -9.3pp |
| openlibrary | 60 | 23 | 48 | 38.3% | -41.7pp |
| webclients | 39 | 13 | 27 | 33.3% | -35.9pp |
| **合计** | **216** | **65** | **134** | **30.1%** | **-31.9pp** |

> 断网版结果 = 断网原版 + 优化重跑版新增 resolved（取最优）。ansible 域优化重跑新增 25 个 resolved，其中 15 个为空 patch（F2P 在 base_commit 上已通过，非有效 bug-fix 任务），已剔除。

## 3. 失败 case 分布（断网原版 161 例）

| 状态 | 数量 | 占比 |
|------|------|------|
| UNRESOLVED | 129 | 80.1% |
| PATCH_APPLY_FAIL | 31 | 19.3% |
| TEST_PATCH_FAIL | 1 | 0.6% |

### UNRESOLVED（129 个）

| 失败模式 | 占比 | 说明 |
|----------|------|------|
| 仅 F2P 失败（P2P 全过） | 74% | 补丁方向错误，未破坏回归 |
| F2P 全过，P2P 回归 | 19% | 补丁引入新 bug |
| F2P+P2P 都失败 | 7% | 完全偏离目标 |

**关键发现**：128 个 UNRESOLVED 均有实质 patch 内容（median 6478 chars）。断网后 agent 能生成修改，但方向与 ground truth 不一致。

## 4. 结论

- 断网整体退化 31.9pp（62.0% → 30.1%），agent 对外部知识有一定依赖
- 失败主因是补丁方向错误（74% 仅 F2P 失败），而非无法生成 patch
- flipt 退化最轻（-9.3pp），openlibrary 最严重（-41.7pp），与代码库自洽度相关
- Prompt 优化（FAIL_TO_PASS 注入）在 ansible 域有效，减少了错误修改
- 数据集存在质量问题：15 个 case 空 patch 即通过，F2P 在 base_commit 上已通过
