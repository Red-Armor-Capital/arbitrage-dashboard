# 本地运行的数据采集口径

- 用户说“不运行分钟预测归档采集器”时，只设置 `CARRY_PREDICTION_COLLECTION_ENABLED=false`。
- 普通实时行情与历史刷新仍应保留默认 `CARRY_ENABLED_VENUES`，除非用户明确要求完全离线或禁止所有外部行情请求。
- 启动后同时验证 `/api/prediction-collector/status` 的 `enabled=false`，以及 `/api/dashboard` 已出现平台状态或机会数据。
