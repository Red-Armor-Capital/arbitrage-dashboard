# Equity Carry Monitor

股票永续跨平台资金费 Carry 与同市场股票现货—链上永续 Carry 实时看板。项目只做行情采集、已结算资金费归档和研究统计；不包含自动下单。

## 当前能力

- 从链上平台官方分类或复核目录自动发现股票/ETF 永续，并统一映射到底层代码；CEX 继续按配置的关注列表采集。
- 每 30 秒采集当前 mark/index/BBO 和下一期指示资金费率。
- 将五个 DEX 的有效预测快照按 UTC 分钟聚合为 OHLC，保留原始单位、原始 tenor、目标结算时刻、价格和转换版本；不对缺失分钟做前值填充。
- 分钟数据在 DuckDB 中至少保留 90 天，完整旧月份经校验后归档为永久 ZSTD Parquet。
- 将已结算资金费写入 DuckDB，按共同小时窗口比较两个平台；历史回填按标的独立跟踪，失败标的单独重试。
- 对精确映射的链上永续生成“多同市场股票现货、空永续”研究候选，并展示公开现货报价与永续 mark/index 的折溢价；当前指示 Carry 为负时仍保留历史观察候选，美股、韩股、港股和日股均按唯一上市证券身份匹配。
- 合约映射使用精确 `(DEX venue, contract symbol) → security_id` 注册表；本地普通股、ADR/ADS、不同股份类别和同公司其他上市地不会互相回退，未复核合约 fail-closed。
- 展示当前预测 Carry、7 日已结算均值、年化波动率、正 Carry 占比、往返 taker 费和手续费回本时间。
- 展示每条永续腿的 24 小时报价币成交额与按 `OI × mark` 估算的美元名义持仓量；现货—永续按单条永续腿比较，永续—永续按较弱腿排序。
- 当前预测年化、7 日已结算均值、年化波动率和正 Carry 占比支持表头升降序排序。
- “最低 7D 已结算年化”只过滤已结算均值，空值表示不设下限；“只看当前正 Carry”作为独立开关，不会用当前预测值替代历史均值。
- 可按用户实际拥有账户的 DEX 多选过滤机会，并在浏览器本地保存选择；股票现货腿不受 DEX 账户筛选影响。
- 预测值与已结算值严格分离；不足 24 小时重叠样本的组合不会标为“稳定”。
- 对跨平台价格偏差超过 10% 的组合做安全过滤，避免把合约乘数或错误 symbol mapping 当成套利。
- 只减仓合约不会生成新开仓套利候选；行情或 FX 失败会立即令对应现货报价失效，不沿用旧价继续计算。

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

分钟采集器依赖单写入器和事务 watermark，请保持 `--workers 1`。

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
- `CARRY_US_EQUITY_REFRESH_SECONDS`：公开股票和 FX 行情刷新间隔，默认 60 秒。
- `CARRY_US_EQUITY_BATCH_SIZE`：每轮美股报价批量大小，默认 12；BB 与 SKHY 每轮优先刷新。KR/HK/JP 按市场独立刷新。
- `CARRY_HISTORY_LOOKBACK_DAYS`：统计窗口。
- `CARRY_DATABASE_PATH`：DuckDB 路径。
- `CARRY_FRONTEND_ORIGINS`：生产前端允许访问 API 的精确 Origin；本地 `localhost`/`127.0.0.1` 任意端口默认由 `CARRY_FRONTEND_ORIGIN_REGEX` 放行。
- `CARRY_CURRENT_MARKET_MAX_AGE_SECONDS`：永续实时快照最大年龄，默认 120 秒；离线来源与超过该阈值的旧快照不参与机会计算。
- `CARRY_PREDICTION_COLLECTION_ENABLED`：是否启用五个 DEX 的分钟预测采集，仓库默认关闭；需要长期采集的实例应显式设为 `true`。
- `CARRY_PREDICTION_HOT_DAYS`：DuckDB 至少保留的分钟数据天数，默认 90。
- `CARRY_PREDICTION_ARCHIVE_DIR`：永久月度 Parquet 归档目录。
- `CARRY_PREDICTION_STATUS_WINDOW_MINUTES`：采集覆盖率状态窗口，默认 60 分钟。

公开 API 会受到所在地区网络策略影响。单个平台暂时离线时，后端会保留已有历史并继续刷新其他平台；状态不会伪装成健康。

## 指标口径

永续—永续假设在低资金费平台做多、在高资金费平台做空，小时 Carry 为：

```text
hourly_carry = short_funding / short_interval - long_funding / long_interval
simple_apr = hourly_carry × 24 × 365
```

股票现货—链上永续只在永续当前资金费为正、且存在明确同市场映射和有效报价时生成：

```text
comparable_spot_usd = local_spot_price / local_currency_per_usd × spot_units_per_perp_unit
contract_basis = (perp_mark_or_index / comparable_spot_usd - 1) × 100
spot_funding = 0
hourly_carry = short_perp_funding / short_perp_interval
simple_apr = hourly_carry × 24 × 365
```

BB 等同股同单位合约按 1:1 比较。`SKHYNIXUSD`、`SKHX` 与 Orderly `SKHYNIX` 只和 KRX `000660.KS` 普通股比较，先用 USD/KRW 将韩元现货换算成美元；`SKHY` 只和美股 `SKHY` ADS 按 1:1 比较。Lighter 的 SK Hynix 产品为 KRW 表现的 quanto 参考，界面会单独标注，折溢价不代表可交割收敛。

港股和日股同样按交易所代码匹配。例如 MiniMax/Zhipu 分别对应 `0100.HK`/`2513.HK`，Kioxia/SoftBank Group 对应 `285A.T`/`9984.T`。Lighter 的 `BYD` 明确对应比亚迪电子 `0285.HK`，不是比亚迪股份 `1211.HK`。`BABA`、`TSM`、`ARM`、`SKHY` 等则使用合约指定的美国 ADS/ADR，不回退到发行人的本地普通股。完整证券与合约清单位于 `backend/app/security_registry.py`。

SpaceX、OpenAI、Anthropic 等私股/预上市合约和 US100/US500 等指数没有可交割的同名股票现货腿，因此不生成现货—永续机会；已下架、身份/单位未核验的合约同样排除。trade.xyz 的 QNT 分类端点存在短暂滞后，注册表只对已复核的 Nasdaq QNT 做显式覆盖，不放宽其他 pre-IPO 标的。

其中：

- `当前预测年化` 使用各平台当前/预计下一期 funding，只用于发现机会，会在结算前变化。
- 永续—永续的 `7D 已结算均值`、波动率和正 Carry 占比只使用双方实际已结算历史的重叠小时。
- 股票现货—永续的历史统计只使用空头永续的实际已结算 funding，现货 funding 始终视为 0；不会用当前预测伪造历史。
- Hotstuff 产品页面会展示 8 小时口径，但公开 ticker API 的 `funding_rate` 已是实际小时支付率，本项目按 1 小时 decimal 原值使用；历史统计使用公开账户的精确已结算 Funding 支付记录，并对重复或冲突记录做安全校验。
- 离散结算的 N 小时资金费会向前展开到其覆盖的 N 个小时，避免把上一期结算率泄漏到下一期。
- `往返手续费 = 2 × (多头平台 taker 费 + 空头平台 taker 费)`，即两边开仓和两边平仓。
- 股票现货—永续目前仅计 `2 × 空头永续 taker 费`；现货佣金、融资/现金机会成本、滑点、税费和过夜费用尚未计入。
- `手续费回本` 优先使用已结算均值；没有历史时可显示按当前预测得到的估计，并明确标注。
- `24h 成交额` 反映近期交易活跃度，`OI 名义` 反映市场存量规模；二者均不是盘口深度或实际可成交性评分。

当前版本尚未计入滑点、盘口深度、稳定币/桥风险、保证金占用、清算风险、税费、券商融资利息、过夜类持仓费和 API 行情授权成本。美股价格来自 best-effort 公开行情（Nasdaq 页面行情，失败时回退 Yahoo chart）；韩股来自 Naver Finance（失败时回退 Yahoo chart），USD/KRW 优先使用 Naver 的 Hana Bank 公示参考；港股、日股及对应 HKD/JPY 汇率使用 Yahoo chart。香港、日本公开源按延迟/延迟状态未知处理。它们都不是券商可成交 bid/ask 或 FX；界面会展示证券代码、MIC、来源、时段和延迟状态，不能直接视为可成交套利。

## 数据与 API

- DuckDB：`data/carry.duckdb`
- `GET /api/dashboard`：汇总、机会列表、平台状态。
- `GET /api/venues`：平台连接状态。
- `GET /api/prediction-collector/status`：分钟采集、覆盖率、热数据和归档状态。
- `POST /api/refresh`：手动触发一次公开数据刷新。

数据库表：

- `instruments`：标的、手续费、结算周期和原始元数据。
- `current_market`：最新行情与指示资金费。
- `funding_rates`：`current` 与 `settled` 分类型保存。
- `venue_status`：健康状态、错误、延迟和标的数量。
- `funding_prediction_minutes`：无索引的分钟预测热表。
- `funding_prediction_collector_status`：各 DEX 采集水位、覆盖率和错误。
- `funding_prediction_archives`：已验证月度 Parquet 清单。
- `schema_migrations`：数据库迁移版本。

永久归档按 `year=YYYY/month=MM/funding_predictions.parquet` 保存。归档通过 schema、行数和时间范围校验并登记清单后，才会删除对应热表月份；归档文件不会自动覆盖或删除。

## 验证

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q backend
npm run build
```

## 风险说明

这是研究工具，不是收益承诺。资金费在结算前可能改变；持仓是否参与某次结算、最终费率锁定方式、指数源、交易时段和异常行情规则均以具体平台为准。交易前应再次读取账户实际费率、合约规格和最终结算规则。
