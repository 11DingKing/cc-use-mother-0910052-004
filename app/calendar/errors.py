"""日历子系统异常类型。"""

from app.middleware.exception_handler import AppException


class CalendarValidationError(AppException):
    """日历配置内容非法（解析期、构建视图期均可抛出）。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            message=message,
            code="CALENDAR_VALIDATION_ERROR",
            status_code=400,
            details=details or {},
        )


class ImmutableVersionError(AppException):
    """试图修改已发布/已封版的不可变版本。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(
            message=message,
            code="IMMUTABLE_VERSION_ERROR",
            status_code=409,
            details=details or {},
        )
