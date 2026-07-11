# Equity Carry Monitor

美股永续跨平台资金费 Carry 与合成现货—链上永续 Carry 实时看板。项目只做行情采集、已结算资金费归档和研究统计；不包含自动下单。

## 当前能力

- 从链上平台官方分类或复核目录自动发现股票/ETF 永续，并统一映射到底层代码；CEX 继续按配置的关注列表采集。
- 每 30 秒采集当前 mark/index/BBO 和下一期指示资金费率。
- 将已结算资金费写入 DuckDB，按共同小时窗口比较两个平台；历史回填按标的独立跟踪，失败标的单独重试。
- 对正资金费链上永续生成“多合成现货、空永续”研究候选；现货价格明确假设为与该永续 mark/index 同价，Funding 为 0。
- 展示当前预测 Carry、7 日已结算均值、年化波动率、正 Carry 占比、往返 taker 费和手续费回本时间。
- 当前预测年化、7 日已结算均值、年化波动率和正 Carry 占比支持表头升降序排序。
- 预测值与已结算值严格分离；不足 24 小时重叠样本的组合不会标为“稳定”。
- 对跨平台价格偏差超过 10% 的组合做安全过滤，避免把合约乘数或错误 symbol mapping 当成套利。

已实现适配器：Binance、Bitget、Bybit、Gate、Kraken、OKX、Lighter、Extended、trade[XYZ]/Hyperliquid、Hotstuff、Orderly。

平台 API、费用、限流和字段口径见 [research/platform_api_matrix.md](research/platform_api_matrix.md)。

## 运行

要求 Node.js 22+ 和 Python 3.12+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
npm ci
```

终端一：

```bash
PYTHONPATH=. .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

终端二：

```bash
npm run dev
```

浏览器打开 `http://localhost:3000`。API 健康检查位于 `http://127.0.0.1:8000/health`。

## 配置

复制 `.env.example` 为 `.env`，可调整：

- `CARRY_ENABLED_VENUES`：启用的数据源。
- `CARRY_CORE_UNDERLYINGS`：CEX 适配器关注的股票代码；DEX 股票目录由平台分类或复核目录发现。
- `CARRY_REFRESH_SECONDS`：实时刷新间隔。
- `CARRY_HISTORY_LOOKBACK_DAYS`：统计窗口。
- `CARRY_DATABASE_PATH`：DuckDB 路径。

公开 API 会受到所在地区网络策略影响。单个平台暂时离线时，后端会保留已有历史并继续刷新其他平台；状态不会伪装成健康。

## 指标口径

永续—永续假设在低资金费平台做多、在高资金费平台做空，小时 Carry 为：

```text
hourly_carry = short_funding / short_interval - long_funding / long_interval
simple_apr = hourly_carry × 24 × 365
```

合成现货—链上永续只在永续当前资金费为正时生成：

```text
synthetic_spot_price = perp_mark_or_index_price
synthetic_spot_funding = 0
hourly_carry = short_perp_funding / short_perp_interval
simple_apr = hourly_carry × 24 × 365
```

其中：

- `当前预测年化` 使用各平台当前/预计下一期 funding，只用于发现机会，会在结算前变化。
- 永续—永续的 `7D 已结算均值`、波动率和正 Carry 占比只使用双方实际已结算历史的重叠小时。
- 合成现货—永续的历史统计只使用空头永续的实际已结算 funding，合成现货 funding 始终视为 0；不会用当前预测伪造历史。
- Hotstuff ticker 的 8 小时展示费率会先换算为小时费率；历史统计使用公开账户的精确已结算 Funding 支付记录，并对重复或冲突记录做安全校验。
- 离散结算的 N 小时资金费会向前展开到其覆盖的 N 个小时，避免把上一期结算率泄漏到下一期。
- `往返手续费 = 2 × (多头平台 taker 费 + 空头平台 taker 费)`，即两边开仓和两边平仓。
- 合成现货—永续目前仅计 `2 × 空头永续 taker 费`；由于没有真实券商腿，现货佣金、融资/现金机会成本、滑点、税费、过夜费用和基差均暂按 0，并在 API/UI 中标为假设口径。
- `手续费回本` 优先使用已结算均值；没有历史时可显示按当前预测得到的估计，并明确标注。

当前版本尚未计入滑点、盘口深度、稳定币/桥风险、保证金占用、清算风险、税费、券商融资利息、过夜类持仓费和 API 行情授权成本。合成现货不是 Moomoo、IBKR 或 Schwab 的真实报价，不能直接视为可成交套利。

## 数据与 API

- DuckDB：`data/carry.duckdb`
- `GET /api/dashboard`：汇总、机会列表、平台状态。
- `GET /api/venues`：平台连接状态。
- `POST /api/refresh`：手动触发一次公开数据刷新。

数据库表：

- `instruments`：标的、手续费、结算周期和原始元数据。
- `current_market`：最新行情与指示资金费。
- `funding_rates`：`current` 与 `settled` 分类型保存。
- `venue_status`：健康状态、错误、延迟和标的数量。

## 验证

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q backend
npm run build
```

## 风险说明

这是研究工具，不是收益承诺。资金费在结算前可能改变；持仓是否参与某次结算、最终费率锁定方式、指数源、交易时段和异常行情规则均以具体平台为准。交易前应再次读取账户实际费率、合约规格和最终结算规则。
