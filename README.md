# 交易日历与企业行动业务服务

这是一个使用 Python、FastAPI 与 SQLite 实现的纯后端业务服务，包含领域模型、数据访问、业务编排、接口和异常路径测试。项目可在单个 Linux 应用容器内离线运行，使用本地 SQLite 或内存替身，不依赖外部运行服务。

## 安装

```bash
python3 -m pip install -r requirements.txt
```

## 测试

```bash
python3 -m pytest -q
```

## 构建检查

```bash
python3 -m compileall -q app
```

## API 导入冒烟

```bash
python3 -c "from app.main import app; print(len(app.routes))"
```

## 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 交易日历与企业行动

下单、回测、日终估值与历史查询不再把自然日当作交易日，而是统一通过
`MarketDataProvider` 使用**同一套生效版本**的交易日历与企业行动数据。

### 本地配置

- 内置日历（随仓库发布、只读）：`data/calendar/{CN,US}.json`，
  含 2024 年度节假日与美股半日市；
- 运行时补录（不入库、不入镜像）：`data/state/calendar/`（临时休市/补班）、
  `data/state/actions/`（分红送转批次），可用环境变量
  `CALENDAR_CONFIG_DIR`、`CALENDAR_STATE_DIR`、`CORP_ACTION_STATE_DIR` 覆盖；
- `CALENDAR_ENABLED=false` 可回退为“每日皆交易日”的宽松内置日历。

### 版本化与不可变

- 日历按市场维护一条**线性版本链**；临时休市、补班只能追加新版本
  （`POST /api/market-data/calendar/special-session`），旧版本永不改写，
  历史账本可固定旧版本复现；
- 每条版本带 SHA-256 内容哈希，文件被非发布流程改动会在载入时被拒绝；
- 企业行动以**批次**发布（`POST /api/market-data/actions/batches`）：
  重复导入完全幂等，同一除权日同类型行动数值冲突会被拒绝。

### 调整可解释

- 休市日信号/订单顺延到下一交易日，订单与回测 `data_notes` 中记录
  `requested_day → trading_day` 及原因（周末/节假日/临时休市）；
- 除权除息在除权日开盘前作用于在仓持仓，每次调整都落一条明细
  （股数/成本/现金分红前后值、方案说明、前复权乘子）；
- 回测结果保存 `market_manifest`（日历版本哈希 + 企业行动批次哈希）、
  `input_fingerprint`（输入与版本指纹的确定性哈希）、`adjustments` 与
  `data_notes`。

### 封账与复现

- 同输入指纹重跑回测会复用原账本（响应含 `reused: true`），不新建、不改写；
- `POST /api/backtest/{id}/seal` 封账后，历史结果成为事实，任何重跑都返回原账本；
- 日历升级后默认用新版本产生新账本，旧账本原样保留；传 `calendar_version`
  可显式固定旧版本复现历史结果。

### 主要接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/market-data/calendar/{market}/day/{day}` | 某日市态与顺延结果 |
| `GET /api/market-data/calendar/{market}/trading-days` | 区间交易日枚举 |
| `GET /api/market-data/calendar/{market}/versions` | 日历版本链 |
| `POST /api/market-data/calendar/special-session` | 补录临时休市/补班（新版本） |
| `POST /api/market-data/calendar/versions` | 发布完整日历新版本 |
| `GET/POST /api/market-data/actions[/batches]` | 企业行动查询与批次发布 |
| `GET /api/market-data/manifest` | 当前生效数据集指纹 |
| `POST /api/trading/apply-corporate-actions` | 对在仓持仓应用除权除息（幂等） |
| `POST /api/trading/mark-to-market` | 日终估值（带版本指纹） |
| `POST /api/backtest/{id}/seal` | 回测账本封账 |
