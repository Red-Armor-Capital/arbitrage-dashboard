# 美股 Carry 平台 API、费用与限制矩阵

更新时间：2026-07-11（Asia/Shanghai）

范围：本阶段只研究现货/永续对冲及跨永续资金费 Carry，不做结算周期错位策略，也不实现自动下单。费率、标的和地域可用性都可能变化；公开默认费率仅用于初筛，投入资金前必须读取账户实际 fee endpoint 或券商账户页面。

## 1. 最重要的数据口径

结算前显示的资金费率通常不是已经锁定的收益。

- `indicative / predicted / current`：根据本期已发生的溢价、利率和风控参数计算的下一期指示值，结算前仍可能改变。
- `settled / realized / applied`：结算窗口结束后实际用于资金费支付的费率。
- 多数离散结算永续在结算时点对当时持仓应用最终费率；费率本身通常由此前一段窗口的溢价样本计算，并非简单读取结算那一秒的瞬时显示值。
- Kraken Flexible Futures 等产品采用连续计提/按小时入账的不同机制，不能与离散 8 小时产品机械等同。

本项目因此采用两条完全分开的数据链：

1. 当前预测只计算 `current_carry_apr`，用于发现候选机会。
2. 永续—永续的均值、波动率、正 Carry 占比和“稳定”标签只使用双方 `settled` 历史的重叠小时；合成现货—永续只使用该永续自己的 `settled` 历史，现货 funding 视为 0。
3. 没有历史时，历史字段返回空；若展示回本时间，必须标为“按当前预测”。
4. 至少 24 个重叠已结算小时才允许标为稳定。

## 2. 中心化交易所

### 总览

| 平台 | 美股永续 list | 当前行情与预测资金费 | 已结算资金费 | 手续费来源 | 公开限制/注意点 | 本地适配 |
|---|---|---|---|---|---|---|
| Binance | `GET /fapi/v1/exchangeInfo`，筛 `status=TRADING`、`contractType=TRADIFI_PERPETUAL` | `/fapi/v1/ticker/bookTicker`、`/fapi/v1/premiumIndex`、`/fapi/v1/fundingInfo` | `/fapi/v1/fundingRate`，最多 1,000 条后分页 | 默认 maker 0.02%、taker 0.05%；账户实际值 `/fapi/v1/commissionRate` | 公共接口按 weight；stock mapping 不能只靠 `TRADIFI`，需本地 underlying 校验 | 已实现；当前网络环境超时 |
| Bitget UTA v3 | `GET /api/v3/market/instruments?category=USDT-FUTURES`，筛 `symbolType=stock`、`type=perpetual`、`status=online` | `/api/v3/market/tickers`、`/api/v3/market/current-fund-rate` | `/api/v3/market/history-fund-rate`，每页最多 100 | 默认 maker 0.02%、taker 0.06%；账户实际值 `/api/v3/account/fee-rate` | 公共接口通常 20 次/秒/IP；stock funding cap 可能动态调整 | 已实现；当前网络环境拒绝连接 |
| Bybit | `GET /v5/market/instruments-info?category=linear&symbolType=stock&status=Trading&limit=1000`，跟随 cursor | `/v5/market/tickers` | `/v5/market/funding/history`，每页最多 200 | 当前 Global TradFi G9 参考 maker 0%、taker 0.0275%；实际值 `/v5/account/fee-rate` | linear 标的超过单页时必须分页；interval/cap 来自 instrument | 已实现；当前网络环境超时 |
| Gate | `GET /api/v4/futures/usdt/contracts`，筛 `contract_type=stocks`、`status=trading` | `/futures/usdt/tickers`、`/contracts/{contract}`、order book；区分 `funding_rate` 与 `funding_rate_indicative` | `/futures/usdt/funding_rate?contract=...`，返回 `t/r` | 优先使用 contract 返回的 maker/taker；账户值 `/futures/usdt/fee` | 约 200 次/10 秒/endpoint/IP；小数合约需 `X-Gate-Size-Decimal: 1` | 已实现并实时验证 |
| Kraken | `/derivatives/api/v3/instruments?contractType=flexible_futures&expired=false`，筛 `tradfi=true`、`tradeable=true` | `/derivatives/api/v3/tickers`、orderbook；应使用 `relativeFundingRate` | `/derivatives/api/v3/historical-funding-rates`，使用 `relativeFundingRate` | 参考 maker 0.02%、taker 0.05% | ticker 的 `fundingRate` 是绝对价格量，不可直接当百分比；Flexible Futures 为连续计提 | 已实现；当前网络环境超时 |
| OKX | `GET /api/v5/public/instruments?instType=SWAP`，筛 `groupId=6`、`instCategory=3`、`state=live` | ticker/books/mark/index；`/api/v5/public/funding-rate` | `/api/v5/public/funding-rate-history`，使用 `realizedRate`，最多 400，公开历史约 3 个月 | `/api/v5/account/trade-fee?instType=SWAP&groupId=6`；负数代表费用、正数代表返佣 | 动态 8/4/2/1 小时；不要把 `fundingRate` 当 `realizedRate` | 已实现；当前网络环境超时 |

### 字段与费用细节

#### Binance

- list 接口没有可靠的“只含美国股票”开关；`TRADIFI_PERPETUAL` 还可能包括其他传统资产，必须再做 underlying/category allowlist。
- `premiumIndex.lastFundingRate` 在看板中一律作为当前指示值；收益统计只读取 funding history。
- `/fapi/v1/fundingInfo` 只返回被单独调整过 cap/floor/interval 的合约；未返回不代表没有 funding。
- 官方文档：[Exchange Information](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)、[Mark Price](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price)、[Funding History](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)。

#### Bitget

- UTA v3 的 stock filter 比旧合约 API 更直接；ticker、current funding、history funding 可完全用公开接口。
- 当前参考 base interest 为 0；股票合约 cap/interval 可能按公告调整，不能硬编码。
- 官方文档：[Futures API](https://www.bitget.com/api-doc/contract/intro)、[Current Funding](https://www.bitget.com/api-doc/uta/public/Get-Current-Funding-Rate)。

#### Bybit

- `fundingInterval` 以分钟返回，必须逐合约读取；`upperFundingRate`、`lowerFundingRate` 同样来自 instrument。
- 当前 G9 费率属于产品/费组参考值，用户等级和活动可能改变，实际计算应覆盖为账户 API 返回值。
- 官方文档：[Instruments](https://bybit-exchange.github.io/docs/v5/market/instrument)、[Tickers](https://bybit-exchange.github.io/docs/v5/market/tickers)、[Funding History](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)、[Account Fee](https://bybit-exchange.github.io/docs/v5/account/fee-rate)。

#### Gate

- contract payload 直接给出 `maker_fee_rate`、`taker_fee_rate`、`funding_interval`、`funding_next_apply`、`funding_rate_limit`，比写死全平台默认值可靠。
- 本轮实时股票合约的 taker 值与常见全平台默认费率存在差异，项目已优先存 contract 实际值。
- 官方文档：[Gate Futures API](https://www.gate.com/docs/developers/apiv4/en/futures/)、[Futures WebSocket](https://www.gate.com/docs/developers/futures/)。

#### Kraken

- REST ticker 同时可能给 absolute 和 relative funding；跨平台年化只能使用 relative rate。
- Flexible Futures 的前一小时费率在下一小时应用并连续计提，平仓/仓位变化也可能触发入账；后续若做实盘 PnL 对账，应按该机制单列。
- 官方文档：[Futures API](https://docs.kraken.com/api/docs/futures-api/)、[Market Analytics](https://docs.kraken.com/api/docs/futures-api/charts/market-analytics)。

#### OKX

- current endpoint 需区分 `fundingRate`、`settFundingRate` 和 `settState`；history 必须优先 `realizedRate`。
- `groupId=6` 是股票永续 fee group；实际账户接口的符号方向与多数平台相反，需规范化后入库。
- 官方文档：[OKX API v5](https://www.okx.com/docs-v5/en/)。

## 3. 去中心化/链上平台

| 平台 | list/分类 | 当前行情与预测 funding | 已结算历史 | 参考 maker/taker | 限制与注意点 | 本地适配 |
|---|---|---|---|---|---|---|
| Lighter | `GET /api/v1/orderBooks` | `/orderBookDetails`、`/orderBookOrders`、`/funding-rates` | `/api/v1/fundings`，最多约 750 | 标准账户 0%/0%；Premium 0.004%/0.028% | 标准 REST 约 60/min；current rate 是标准化 8h 值，历史 `rate` 是百分数且带 `direction` | 已实现并实时验证 |
| Extended | `GET /api/v1/info/markets`，筛 `category=TradFi`、`subCategory=Equity` | 同一响应的 `marketStats` 含 BBO/mark/index/funding | `/api/v1/info/{market}/funding`，最多 10,000，支持 cursor | 0%/0.025% | 约 1,000/min/IP；`name` 才是 API market id，不能用 UI name | 已实现并实时验证 |
| trade[XYZ] / Hyperliquid HIP-3 | `POST /info`：`perpCategories` + `metaAndAssetCtxs(dex=xyz)` | context 含 funding/oracle/mark/mid/impact/OI/volume；`l2Book` 可取盘口 | `fundingHistory`，每次最多 500 | Growth Mode 0.003%/0.009%；标准 HIP-3 0.03%/0.09% | 1,200 weighted/min/IP；symbol 必须保留 `xyz:` 前缀 | 已实现并实时验证 |
| Hotstuff | `POST /info`，method=`instruments` | method=`ticker`，含 BBO/mark/index/current funding | `funding_history` 是账户级精确支付历史；以已覆盖全部股票/ETF 的公共观察账户抽取，并逐笔校验 payment | -0.002%/0.025% | `/info` 约 5,000/min；公共账户没有持仓的小时会自然缺失，不能视为 market-wide 完整历史 | 已实现并实时验证 |
| Orderly | `GET /v1/public/info` | `/v1/public/futures` + `/v1/public/funding_rates` | `/v1/public/funding_rate_history` | base 0%/0.05%；builder 可能覆盖 | 公开 funding 接口 10/s/IP；stock 当前保守纳入原生 GOOGL/TSLA/NVDA | 已实现并实时验证 |
| Aster | `/fapi/v3/exchangeInfo`、bookTicker、premiumIndex | Binance-like market API | funding history | 0%/0.04% | 文档 symbol 示例冲突且当前环境可能受 WAF/地区限制，必须以 live exchangeInfo 为准 | 研究完成，暂未启用 |

### 特殊单位转换

- Lighter `/funding-rates` 返回标准化 8 小时 rate，本项目换算为小时值时除以 8。
- Lighter `/fundings` 的 `rate` 是 percent unit，例如 `0.0004` 表示 0.0004%，转 decimal 还需除以 100；正负由 `direction` 决定。
- Hyperliquid/XYZ context 和 `fundingHistory` 均按小时 decimal rate 处理。
- Extended 历史文档明确说明记录是实际用于每小时 funding payment 的 rate，可直接作为 settled。
- Hotstuff 产品文档区分 8 小时展示率与小时支付率，但 ticker API 的 `funding_rate` 实测已与账户支付记录中的小时 rate 同口径，应按 1 小时 decimal 原值使用，不能再次除以 8。
- Orderly `est_funding_rate` 是预测；`last_funding_rate` 和 history 是结算记录。股票原生合约当前多为 8 小时，但应以 `funding_period` 动态读取。

官方资料：

- [Extended API](https://api.docs.extended.exchange/)
- [Hyperliquid Info Endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [Hotstuff Funding Rates](https://docs.hotstuff.trade/hotstuff-docs/trading/funding-rates)
- [Orderly Predicted Funding](https://orderly.network/docs/build-on-omnichain/restful-api/public/get-predicted-funding-rates-for-all-markets)
- [Orderly Funding History](https://orderly.network/docs/build-on-omnichain/restful-api/public/get-funding-rate-history-for-one-market)
- [Orderly Funding Rules](https://orderly.network/docs/introduction/trade-on-orderly/perpetual-futures/funding-rate)
- [Lighter API](https://apidocs.lighter.xyz/)
- [Hotstuff Public API](https://api.hotstuff.trade/info)

## 4. 正规券商现货腿

这里必须区分两种“只读”：

1. 券商用户名本身没有任何交易权限。
2. 用户名可交易，但看板进程/API App 被技术上禁止下单。

| 平台 | 用户名本身无交易权限仍可取行情 | 可让看板进程无下单能力 | 推荐接入 | list/search | BBO/实时 | 主要限制 |
|---|---|---|---|---|---|---|
| Moomoo | 可以 | 可以，隔离最彻底 | 独立未开户 Moomoo ID + 独立 OpenD | `get_stock_basicinfo(Market.US, STOCK)`；无独立模糊 search，本地索引 | snapshot 或订阅 `ORDER_BOOK` | 默认 100 个订阅单位；API 源不是完整 SIP NBBO |
| IBKR | 不可以，`/iserver` 行情要求 trading-enabled brokerage session | 可以，TWS/IB Gateway 勾选 `Read-Only API` | 本地 IB Gateway/TWS socket | `/trsrv/all-conids`、`/trsrv/stocks`、`/iserver/secdef/search` | snapshot/WebSocket；TWS `reqMktData`/tick-by-tick | Web API 10 req/s；需要开户、入金、产品交易权限和行情 entitlement |
| Schwab | 未找到官方无交易券商用户路径 | 可以，App 只订阅 `Market Data Production` | 云端 OAuth + 独立 Market-Data-only App | `/marketdata/v1/instruments`；无公开确认的全量 master list | REST quotes + `LEVELONE_EQUITIES` stream | App 审批后才能看到完整 OpenAPI spec/数值限流；不得公开转发行情 |

### Moomoo

- OpenD 默认 `127.0.0.1:11111`；v10.8 允许仅注册 Moomoo ID、未开户用户登录，首次完成 API 问卷和协议。
- 强只读建议：独立未开户 ID、独立 OpenD、只创建 `OpenQuoteContext`，不保存交易密码、不调用 `unlock_trade`。解锁状态属于整个 OpenD 进程，因此不能与交易程序共享。
- 全量美股：`get_stock_basicinfo`。Snapshot 单次最多 400 标的、60 次/30 秒；历史首请求 60 次/30 秒，后续分页不计。
- 默认注册用户：100 个实时订阅单位和 100 个过去 7 日不同历史标的额度；订阅按“标的 × 数据类型”计。
- 当前 US LV3 促销源为 Nasdaq Basic、Nasdaq TotalView 和 NYSE ArcaBook，不应标成 consolidated NBBO。
- 费用必须按开户实体：Moomoo US 美国居民普通美股当前佣金/平台费为 0，USD margin 页面显示约 6.8%；非美国居民、SG、AU 结构不同，不能写死。
- 官方文档：[OpenD](https://openapi.moomoo.com/moomoo-api-doc/en/opend/opend-intro.html)、[权限与配额](https://openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html)、[Static Info](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-static-info.html)、[Snapshot](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-market-snapshot.html)、[Pricing](https://www.moomoo.com/us/pricing)。

### IBKR

- 推荐长期看板使用 TWS/IB Gateway socket；只监听 localhost 并勾选 `Read-Only API`。TWS live/paper 默认 7496/7497，Gateway live/paper 默认 4001/4002。
- 个人 Client Portal Gateway 需同机浏览器用户名/密码/2FA，午夜后至少每日重认证一次，约 5 分钟无请求会超时，不适合完全无人值守。
- Web snapshot 最多 100 conid、50 fields，84/86/88/85 对应 bid/ask/bid size/ask size；首次请求是 pre-flight。历史最多 1,000 点、5 个并发。
- 免费 Cboe One/IEX 不是 consolidated NBBO；套利级现货腿建议订阅 Network A/B/C 或对应 bundle，并确认 API off-platform entitlement。
- IBKR Pro 参考佣金：Fixed `$0.005/股，最低 $1/单`；Tiered `$0.0005–0.0035/股，最低 $0.35/单`，另计交易所/监管费。
- 当前 USD margin 参考：首 `$100k` 约 5.120%，随后按资产档位下降；实际账户分段混合计息。
- 官方文档：[Web API](https://ibkrcampus.com/campus/ibkr-api-page/webapi-doc/)、[TWS API](https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/)、[Market Data Subscriptions](https://ibkrcampus.com/campus/ibkr-api-page/market-data-subscriptions/)、[Commissions](https://www.interactivebrokers.com/en/pricing/commissions-home.php)、[Margin](https://www.interactivebrokers.com/en/accounts/fees/pricing-margin-rates.php)。

### Schwab

- App 只能订阅一个 API Product；为看板单独创建只含 `Market Data Production` 的 App，可实现产品级无订单权限。
- 需要 Developer App 审批和用户 OAuth authorization-code 授权。Token 生命周期必须读取 `expires_in`，不要硬编码社区流传数字。
- 普通美国上市股票/ETF 在线佣金 0；OTC `$6.95`。
- 当前 Base Rate 10.00%，margin debit 从小额账户约 11.825% 起，大额逐级下降；全额现金买入则无 margin debit 利息。
- 完整数值限流和 streaming 配额仅在登录开发者门户后可见，不能把社区常见的 120 req/min 当成官方确认值。
- 官方文档：[Trader API](https://developer.schwab.com/products/trader-api--individual)、[OAuth](https://developer.schwab.com/user-guides/get-started/authenticate-with-oauth)、[Pricing](https://www.schwab.com/pricing)、[Margin](https://www.schwab.com/margin/margin-rates-and-requirements)。

## 5. 看板费用模型

当前版本保守按双方 taker 开仓与平仓：

```text
round_trip_fee = 2 × (long_venue_taker_fee + short_venue_taker_fee)
historical_breakeven_hours = round_trip_fee / mean_realized_hourly_carry
indicative_breakeven_hours = round_trip_fee / current_indicative_hourly_carry
```

当前还会对正资金费链上永续生成研究用途的合成现货—永续组合：

```text
synthetic_spot_price = short_perp_mark_or_index_price
synthetic_spot_funding = 0
spot_perp_carry = short_perp_funding
spot_perp_round_trip_fee = 2 × short_perp_taker_fee
```

这不是实时券商现货行情。合成现货佣金、融资/现金机会成本、滑点、税费、过夜费用和基差暂按 0；API 用 `strategy_type=spot_perp`、`price_assumption=spot_equals_perp` 和 `fee_scope=perp_leg_only` 明示该口径。

后续接入账户凭据后，费用优先级应为：

1. 账户/标的实际 fee endpoint。
2. 公开 contract/instrument 字段。
3. 官方默认 tier。
4. 人工配置，并带生效日期。

券商现货腿还需额外加入：佣金最低收费、监管/结算费、融资利息、做空借券费、股息/预扣税、行情订阅费。现有跨永续看板暂不把这些隐含成本混入同一列。

## 6. 当前验证结果与已知缺口

- 2026-07-11 本机已实时连接 Gate、Lighter、Extended、trade[XYZ]、Hotstuff、Orderly，共 6 个健康公开数据源。
- Binance、Bitget、Bybit、Kraken、OKX 适配器已实现，但本机出口出现超时/拒绝；状态正确显示 offline，不回退到伪数据。
- DuckDB 已取得 Gate/Lighter、XYZ/Lighter、Orderly 等组合的已结算重叠历史；Hotstuff 使用公共观察账户的已验证支付记录，缺失小时保持为空，不伪造 market-wide 历史。
- Aster 先列入 watchlist，待能稳定访问 live `exchangeInfo` 后再启用。
- Moomoo、IBKR、Schwab 尚未接入运行态，因为分别需要 OpenD/券商会话/OAuth App；目前的合成现货腿不冒充这些券商报价。接入真实现货前需要用户提供本机网关或已获批只读凭据，但不需要授予自动交易权限。
- 当前没有计算盘口可成交深度、滑点、保证金效率、清算缓冲、稳定币/桥风险和税务成本；这些是进入实盘评估前的下一层门槛。
