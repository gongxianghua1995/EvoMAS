# 实验通用评估用法

## **环境设置**
1. **操作系统**: Linux
2. **容器工具**: Docker
   - 推荐版本: Docker 20.10及以上
3. **工作目录挂载**: 挂载必要的测试数据集目录与输出目录：
   - 示例: `-v /path/to/dataset:/app/dataset`

4. **依赖安装**:
   - 镜像中安装 `transformers`, `json`, `docker` 等Python包
   - 配置 `mas_runner.py` 文件，满足实验逻辑。

## **通用运行命令**
```bash
docker run \
--rm \
-v /path/to/SWE-bench:/app/dataset \
-v /path/to/EvoMAS/output:/app/output \
my-eval-image:latest \
python3 src/utils/mas_runner.py \
--dataset-path /app/dataset/Pro-SWE-bench.json \
--output-path /app/output \
--step-limit 250 \
--mode eval \
--eval-report-file eval_report.json
```

## **命令说明**
- **`--rm`**: 容器退出后自动删除。
- **`-v`**: 挂载本地数据到容器，路径需自行修改。
- **`--step-limit`**: 每次评估的最大步数，建议依据场景配置 (40~250)。
- **`--mode`**: 运行模式设置为 `eval`。

---

### **通用运行逻辑**

1. **配置初始化**
   - 加载实验配置文件（如 `yaml/json` 等）。
   - 加载所需数据集（分割为 `train` / `test` / `validation` 集）。
   - 初始化运行环境和日志系统。

2. **运行流程**
   - 支持单任务运行：对一个任务处理输入、执行逻辑并保存结果。
   - 支持多任务运行：可指定任务 ID，或按数量选择，并支持并行处理。

3. **结果评估和输出**
   - 自动计时、记录结果（如正确性、时间耗费、Token 使用）。
   - 根据需求实时保存任务的输出和评估信息。
   - 输出完整的 `results.json` 汇总结果。

---

### **范例运行代码**
以下为基于上述逻辑的代码片段：

```python
from pathlib import Path
from multiprocessing import Pool

class ExperimentRunner:
    def __init__(self, config_path, dataset_path, output_dir):
        # 初始化实验配置、数据集和输出路径
        self.config_path = Path(config_path).resolve()
        self.dataset_path = Path(dataset_path).resolve()
        self.output_dir = Path(output_dir).resolve()

    def run_single_task(self, task):
        """运行单个任务，并保存任务结果"""
        try:
            # 实验逻辑（示例）
            result = self._execute_task(task)
            # 保存结果
            self._save_result(task, result)
        except Exception as e:
            print(f"任务 {task} 失败: {e}")

    def run_tasks_parallel(self, tasks, workers=4):
        """并行运行任务"""
        with Pool(processes=workers) as pool:
            pool.map(self.run_single_task, tasks)

    def _execute_task(self, task):
        """实验执行逻辑 - 替换为具体实验逻辑"""
        # 示例：计时 + 模拟计算
        import time
        start_time = time.time()
        result = f"Result of {task}"
        duration = time.time() - start_time
        return {"task": task, "result": result, "duration": duration}

    def _save_result(self, task, result):
        """保存单任务结果到文件"""
        task_output_dir = self.output_dir / "results"
        task_output_dir.mkdir(parents=True, exist_ok=True)
        output_file = task_output_dir / f"{task}.txt"
        with open(output_file, "w") as f:
            f.write(str(result))
```

---

### **使用方法**
```bash
# 示例：运行多任务
runner = ExperimentRunner(config_path="config.yaml", dataset_path="data.json", output_dir="output/")
tasks = ["task1", "task2", "task3"]
runner.run_tasks_parallel(tasks, workers=4)
```

如需更详细调整或者适配其他场景，欢迎进一步提供需求！