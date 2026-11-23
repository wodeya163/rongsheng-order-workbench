#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务异常体系：服务层只抛这些，界面层统一捕获展示。"""


class BusinessError(Exception):
    """所有业务异常基类"""


class NotFoundError(BusinessError):
    """引用的实体不存在"""


class ValidationError(BusinessError):
    """字段校验失败"""


class DuplicateError(BusinessError):
    """唯一约束冲突（单号 / 项号 / 关联重复）"""


class StateError(BusinessError):
    """状态机不允许的迁移"""


class ReferencedError(BusinessError):
    """单据被下游引用，不允许作废 / 删除"""

    def __init__(self, message: str, refs=None):
        self.refs = refs or []
        super().__init__(message)


class OverAllocationError(BusinessError):
    """排产分配数量超过询配码项的可排量"""

    def __init__(self, item_code: str, requested: float, available: float):
        self.item_code = item_code
        self.requested = requested
        self.available = available
        super().__init__(
            f"订单明细 {item_code} 可排量不足：申请 {requested}，剩余 {available}"
        )
