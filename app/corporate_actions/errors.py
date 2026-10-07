"""企业行动子系统异常类型。"""

from app.middleware.exception_handler import AppException


class ActionValidationError(AppException):
    """企业行动数据非法。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            message=message,
            code="ACTION_VALIDATION_ERROR",
            status_code=400,
            details=details or {},
        )


class ImmutableBatchError(AppException):
    """试图改写已发布的企业行动批次。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            message=message,
            code="IMMUTABLE_BATCH_ERROR",
            status_code=409,
            details=details or {},
        )
