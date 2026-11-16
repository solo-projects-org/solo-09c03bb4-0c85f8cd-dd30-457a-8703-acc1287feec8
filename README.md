# 院感药敏耐药统计 Web 应用

面向院感数据员的 FastAPI + SQLite 网页：上传反复送检的药敏 CSV，按患者 + 菌药组合做
30 天首份去重，结合 S/R 切点统计各档计数与 R 占比，并标出 MIC 界值跨档、需要复测的原始记录。

## 业务规则

1. **CSV 列（表头精确匹配，UTF-8）**：`记录号, 修订号, 匿名患者号, UTC采样日, 菌种, 药物, MIC`
   - 修订号为正整数；同一记录号的更高修订号代表更正，旧版本保留修订链但不再参与统计。
   - UTC 采样日接受 `YYYY-MM-DD` 或带时区的 ISO-8601 时间（统一按 UTC 取日期）。
   - MIC 接受正数或带 `<`、`≤`、`>`、`≥`（ASCII 的 `<=`、`>=` 等价）的界值。
2. **切点**：每个菌种 + 药物组合录入正数 S 上限与 R 下限，必须满足 `S < R`。
   S：`MIC ≤ S`；I：`S < MIC < R`；R：`MIC ≥ R`。
3. **30 天去重（按患者 + 菌种 + 药物）**：仅对当前生效记录，按（采样日, 记录号）排序，
   取首份；其后 **30 天内（含第 30 天）** 的记录全部跳过；第 31 天及以后的记录成为下一份
   入选记录并成为新锚点。去重在**全部历史**上计算，查询日期范围**不会重置窗口**。
4. **跨档待复测**：界值 MIC 的可能取值同时落入多个判读档（如 S=4、R=16 时 MIC 为 `>4`）时
   标记“待复测”，给出应复测的记录号；该记录**仍是入选记录、占用去重窗口**，但
   **不计入可判读分母**。未配置切点的组合同样不计入分母。
5. **R 占比** = R 计数 /（S+I+R 可判读分母）；分母为 0 时明确提示“无法计算比例”。
6. **原子性与一致性**：整批 CSV 只要有任何非法行即整体拒绝，不触碰已接纳数据；
   所有写入在 `BEGIN IMMEDIATE` 事务内提交。每次报告在一个只读快照事务内完成，
   更正/切点修改与查询交错时，一次结果只使用同一已提交状态（页面显示快照版本号）。

## 启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据保存在命名卷 `susceptibility-data`（容器内 `/data`），重建容器数据不丢。

## 本地运行（无 Docker 时）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
APP_DB_PATH=./data/susceptibility.db uvicorn app.main:app --reload --port 8000
```

## 操作流程

1. 首页①上传 `sample_data/sample_records.csv`（非法批次会逐行列出错误且不写库）。
2. 首页②录入切点，例如：大肠埃希菌/头孢他啶 S=4、R=16；肺炎克雷伯菌/美罗培南 S=2、R=8。
3. 首页③选择日期范围生成报告：查看各档计数、R 占比、待复测清单、入选/剔除记录号与原因；
   点击“历史”查看任一记录号的修订链。
4. 用更高修订号重新上传同一记录号即可更正；报告快照版本号随之递增。

## JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/upload` | multipart 字段 `file`，全量校验后原子接纳 |
| `POST` | `/api/cutoffs` | JSON `{organism,drug,s_cutoff,r_cutoff}` |
| `GET`  | `/api/cutoffs` | 已录入切点与快照版本 |
| `GET`  | `/api/report?start=&end=&organism=&drug=&patient_id=` | 统计、明细、待复测清单 |
| `GET`  | `/api/records/{记录号}` | 该记录号的完整修订历史 |
| `GET`  | `/health` | 健康检查与当前快照版本 |

## 测试

```bash
. .venv/bin/activate   # 或使用已安装依赖的解释器
python -m pytest tests/ -v
```
