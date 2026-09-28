# Benchmark 测试与验证通用方法

## **简介**
本文件汇总了支持 Benchmark 测试与验证的两种通用方法：
1. **常规评估实验**: 不禁用网络，适用于标准功能验证和性能测试场景。
2. **断网实验**: 完全禁用外部网络访问，适用于模拟网络隔离条件下的功能验证。

---

## **环境基础要求**
1. **操作系统**: Linux
2. **容器工具**: Docker
   - 推荐版本: Docker 20.10及以上
3. **数据挂载**: 本地数据路径需要通过 `-v` 挂载到 Docker 容器内：
   - 数据路径示例：`-v /path/to/dataset:/app/dataset`

---

## **常规评估实验命令**
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

### **命令说明**
- **`--rm`**: 容器退出后自动删除。
- **`-v`**: 挂载本地路径，需指定数据集和输出目录。
- **`--step-limit`**: 最大步数设置，建议根据性能需求调整 (40~250)。
- **`--mode`**: 设置运行模式为 `eval`。
- **`--eval-report-file`**: 为评估报告指定输出文件名。

---

## **断网实验命令**
```bash
docker run \
--network none \
--rm \
-v /path/to/SWE-bench:/app/dataset \
-v /path/to/EvoMAS/output:/app/output \
my-eval-image:latest \
python3 src/utils/mas_runner.py \
--dataset-path /app/dataset/Pro-SWE-bench.json \
--output-path /app/output \
--step-limit 250 \
--mode isolated \
--eval-report-file isolated_eval_report.json
```

### **命令说明**
- **`--network none`**: 禁止容器访问外部网络，以模拟隔离环境。
- 其余参数与 **常规评估实验** 相同。

---

## **实验逻辑概览**
1. **配置初始化**:
   - 加载入口配置：包括任务设置(`yaml/json`) 和数据集路径。
   - 初始化输出目录存储评估结果。

2. **运行与日志**:
   - 支持批量任务运行（多任务并行）。
   - 输出日志包括：任务耗时、Token 使用量、结果汇总等。

3. **结果保存**:
   - 汇总结果到 `results.json` 文件。
   - 支持目录结构下的任务子文件输出（如每任务单独记录）。

---

## **Docker 容器镜像构建示例**
确保你已有 `src` 目录以及依赖的 `requirements.txt` 文件。

```dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY src/ /app/src/
COPY requirements.txt /app/

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python3", "src/utils/mas_runner.py"]
```

构建命令示例：
```bash
docker build -t my-eval-image .
```

---

### **补充说明**

- 两种运行模式主要取决于是否需要网络隔离，其他命令参数一致。
- 建议依据项目需求调整步骤限制（`--step-limit`）和数据路径。

如有需要调整或更多功能集成，请进一步告知！