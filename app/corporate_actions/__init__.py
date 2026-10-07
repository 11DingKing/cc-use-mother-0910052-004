"""企业行动（corporate actions）子系统。

支持的行动：

- ``cash_dividend`` 现金分红：每股派现（税前），除息日调整价格与持仓成本；
- ``stock_dividend`` 送股：每 10 股送 ``ratio_per10`` 股；
- ``split`` 拆股/转增：``ratio`` 股拆为 ``ratio`` 股（10 转 10 即 ratio=2）。

行动以**版本化批次（batch）**导入，批次一经发布即不可变：

- 同一 (股票, 除权日, 类型, 关键参数) 重复导入幂等，不产生新版本、不重复调整；
- 已发布批次不可修改/删除，补录或勘误只能追加新批次；
- 每次应用到订单、持仓、估值时都会留下可追溯的调整说明
  (:class:`AppliedAction`)，解释价格/数量/成本为何变化。
"""

from app.corporate_actions.models import (
    CorporateAction,
    ActionType,
    AppliedAction,
    ActionBatch,
    adjust_quantity,
    apply_action_to_position,
)
from app.corporate_actions.action_store import (
    ActionStore,
    ActionValidationError,
)

__all__ = [
    "CorporateAction",
    "ActionType",
    "AppliedAction",
    "ActionBatch",
    "ActionStore",
    "ActionValidationError",
    "adjust_quantity",
    "apply_action_to_position",
]
