"""市场数据子系统：交易日历与企业行动的统一生效版本入口。

下单、回测、日终估值、历史查询都应通过本模块取日历/行动数据，保证它们依据
**同一套生效版本**，而不是各自硬编码自然日。

环境变量：

- ``CALENDAR_ENABLED``（默认 ``true``）：关闭后回退为“每日皆交易日”的宽松日历；
- ``CALENDAR_CONFIG_DIR`` / ``CALENDAR_STATE_DIR``：日历内置配置与补录状态目录；
- ``CORP_ACTION_STATE_DIR``：企业行动批次目录。
"""

from app.marketdata.provider import (
    MarketDataProvider,
    get_provider,
    resolve_market,
)

__all__ = ["MarketDataProvider", "get_provider", "resolve_market"]
