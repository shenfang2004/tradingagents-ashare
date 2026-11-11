# TradingAgents A 股行情适配

基于 TradingAgents 0.7.1，将腾讯历史日线接入行情分析师。股价、技术指标与校验快照复用本地行情缓存，再交给多空研究员、研究经理、交易员和风险评审完成分析。

## 运行

使用 Python 3.11 或更新版本。先将本仓库与上游 TradingAgents 放在同一目录下，在本仓库根目录执行以下命令。建议在虚拟环境内安装依赖。

```shell
git clone https://github.com/TauricResearch/TradingAgents.git ../TradingAgents
git -C ../TradingAgents checkout 3e44faafa042daf953f6ec2422ae36bb1fd4f50f
python -m pip install -e ../TradingAgents
python run.py --project-dir ../TradingAgents --ticker 600519.SS --date 2026-11-06 --check-data
```

数据检查执行股价、RSI、MACD 和校验快照工具，输出到 `checks/`。选用早于上海时区当日的日期。上交所股票使用 `.SS` 后缀，深交所股票使用 `.SZ` 后缀。

要运行完整的 Agent 流程，按上游 `.env.example` 在 TradingAgents 目录下配置本地模型提供商、模型名和 API 密钥，再执行：

```shell
python run.py --project-dir ../TradingAgents --ticker 600519.SS --date 2026-11-06 --run
```

运行报告位于 `results/`。运行 `python -m unittest -v test_adapter` 可执行离线测试。

## 实现与验证

- 行情请求按股票和分析日缓存。缓存记录来源、抓取时间和价格口径，失败后冷却 60 秒。技术指标由 stockstats 在本地计算。
- 解析并校验 OHLCV，转换成交量单位，并截断分析日以后的记录。50 日和 200 日简单移动平均需要完整窗口，历史长度不足时返回缺失值。
- 2026-11-10 的本地演示取得贵州茅台 486 条历史日线，并生成了行情、多空研究、交易、风险评审和最终决策报告。这是一次指定股票和日期的流程复现。
- 当前适配历史不复权日线，基本面和新闻分析师尚未接入国内数据。除权除息可能影响技术指标和价格收益。当前通过进程内绑定接入 TradingAgents 0.7.1，升级上游版本时需要重新验证接口。

上游项目及许可证见 [TradingAgents](https://github.com/TauricResearch/TradingAgents)。数据接口参考 [腾讯证券日线说明](https://akshare.akfamily.xyz/data/stock/stock_quote.html#stock-zh-a-hist-tx)。
