# 多维分析实验平台

面向实验设计、指标计算、样本分析、效果评估与结论发布的数据分析实验平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实验设计 | `experiment` | 实验方案 | 方案编号、实验名称、对照组说明 |
| 样本分群 | `sample_group` | 样本分群 | 分群编号、分群名称、筛选条件 |
| 指标定义 | `metric_def` | 指标定义 | 指标编号、指标名称、计算公式 |
| AB结果分析 | `ab_result` | 分析报告 | 报告编号、实验编号、分析指标 |
| 分群对比 | `segment_compare` | 对比任务 | 任务编号、分群A、分群B |
| 多维下钻 | `drill_down` | 下钻维度 | 维度编号、维度名称、下钻层级 |
| 数据筛选 | `data_filter` | 筛选规则 | 规则编号、数据来源、筛选字段 |
| 归因分析 | `attr_analysis` | 归因任务 | 任务编号、目标指标、候选因子 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、监控指标、检测算法 |
| 预测模型 | `forecast_model` | 模型配置 | 模型编号、预测对象、历史窗口 |
| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 同期群追踪 | `cohort_track` | 同期群定义 | 群组编号、群组名称、首次行为 |
| 可视化配置 | `chart_config` | 图表配置 | 图表编号、图表类型、数据来源 |
| 洞察报告 | `insight_report` | 洞察报告 | 报告编号、分析主题、数据来源 |
| 数据新鲜度 | `data_freshness` | 新鲜度监控 | 监控编号、数据表名、最新分区 |
| 口径注册 | `metric_alias` | 口径版本 | 版本编号、指标编号、变更说明 |
| 实验因子 | `expr_factor` | 因子配置 | 因子编号、因子名称、因子层级 |
| 评估看板 | `evaluate_board` | 看板配置 | 看板编号、看板名称、关联实验 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 洞察报告版本链路（V008）

洞察报告的状态与结论版本必须同步推进，结论统一放在「洞察结论数据表」
（`insight_conclusion`）里，由 `backend/app/services/insight_conclusion.py` 独家读写。

- **按版本去重聚合**：结论按 `(报告编号, 报告版本)` 覆盖写入；读取时先按结论编号取最新，
  再按 `(指标编号, 指标口径, 关键发现)` 去重后计数。失败重试、重复生成都不会重复累计
  关键发现，历史版本在详情页 `/insight_report/:id` 仍可回看。
- **当前数据版本唯一**：工作台 `/insight_report` 每个报告只输出当前数据版本的一条聚合结果，
  与详情页共用同一份投影，保证两边业务数据一致。
- **状态同步**：`开始生成` → 报告与结论版本同时进入「生成中」；`确认完成` 先落定结论再推进
  报告；`归档报告` 先校验结论完整、口径一致，再同时归档。任一步失败整体回滚。
- **归档失败保留最后完整结论**：归档前保存结论表快照，失败时恢复并把报告标记为「归档失败」；
  重试只清理 `草稿/生成中` 的未清残留，已归档章节永远不会退回草稿。
- **并发冲突**：重算回执（`POST /api/insight_report/{id}/recompute`）与归档请求同时到达时，
  归档动作的 `resolution` 决定顺序：
  `recompute_then_archive`（默认）先完成重算再归档，归档内容包含新结论；
  `archive_then_recompute` 先归档旧结论，再把重算版本登记为「修订」。
- **跨模块联动**：指标定义 V008 通过 `GET /api/metric_def/insight-metrics` 读取结论表指标字段，
  同版本下同一指标编号出现多个口径时返回 409，并拒绝该指标「标记生效」。

结论数据表的只读入口：`GET /api/insight_conclusion?报告编号=&版本=`（默认返回去重聚合）。

领域：数据工程
子方向：数据分析与数据应用
