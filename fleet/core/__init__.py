"""契约层：结果信封 / 设备基类 / 注册表。上层只该 import 这里。"""
from .result import (  # noqa: F401
    CODES, DENIED, ERROR, OK, RETRYABLE, TIMEOUT, UNAVAILABLE, UNSUPPORTED, DeviceResult,
)
from .device import BaseDevice  # noqa: F401
from .registry import Capability, Param, Registry  # noqa: F401
