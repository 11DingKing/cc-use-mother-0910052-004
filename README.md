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

下单、回测、日终估值和历史查询共用同一份**带生效版本**的交易日历与企业行动数据，
实现在 `app/marketdata/`：

- `calendar.py`：时区感知的交易日历（CN/HK/US 默认时区与周末规则可配置），
  支持节假日、临时休市、周末补班覆盖；休市日订单默认拒绝并返回下一交易日，
  回测信号可显式顺延且留下 `order_roll` 调整记录。
- `actions.py`：现金分红、送股、拆股、合股的纯计算引擎。除权除息日调整持仓
  数量、单位成本与现金，每笔调整都带 `before/after`、计算公式与原因；
  另返回除权参考价推导，解释行情价格为何跳变（不篡改原始K线）。
- `calendar_store.py` / `action_store.py`：只追加（append-only）存储。
  重复导入幂等返回 `skipped_duplicate`；补录/参数修正追加新行并以
  `supersedes_id` 指向旧行；撤销同样追加 `revoked` 行。任何历史时间点的
  生效版本都可用 `as_of` 完整还原。
- `registry.py`：统一入口。每次业务运行解析一个 `MarketDataPin`
  （日历版本号 + 日历/企业行动内容指纹 + `as_of`），账本钉住该 pin。
- `seal_store.py`：回测结果写入即封账（`ledger_hash`）。重新运行时按封账时
  的 `as_of` 复现原账本并比对哈希，事后补录的休市/分红不会静默改写历史结果。

### 配置

- `MARKET_DEFAULT_MARKET`：默认市场（默认 `CN`）。
- `MARKETDATA_SEED_FILE`：启动时幂等导入的本地 JSON 种子文件，
  格式见 `marketdata_seed.example.json`，可反复执行。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/market/calendar/overrides` | 导入/修订休市或补班（幂等，返回 inserted/skipped_duplicate/superseded） |
| POST | `/api/market/calendar/revoke` | 撤销某日覆盖（追加撤销行） |
| GET | `/api/market/calendar/status?day=&market=&as_of=` | 查询是否交易日、原因、上下一交易日、版本指纹 |
| GET | `/api/market/calendar/trading-days` | 区间交易日列表 |
| POST | `/api/market/actions` | 导入/修订分红、送股、拆股、合股（`client_id` 必填，幂等） |
| POST | `/api/market/actions/revoke` | 撤销企业行动 |
| GET | `/api/market/actions/explain` | 除权参考价推导（前收→参考价、公式、原因） |
| GET | `/api/market/pin` | 当前/历史时间点的生效版本指纹 |
| POST | `/api/trading/end-of-day/{day}` | 日终估值：应用当日企业行动并重算账户 |
| GET | `/api/trading/adjustments` | 会话内全部价格/数量调整及其原因 |
| POST | `/api/backtest/run` | 支持 `market`、`as_of` 参数钉住版本；响应含 pin、调整明细与封账标记 |
| GET | `/api/backtest/{id}/verify` | 按封账版本重跑并比对账本哈希，验证可复现性 |

跨时区的日期一律先换算到交易所时区再取日期；无时区信息的时间戳按交易所
本地时间处理。
