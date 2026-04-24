"""OpenHarness API 错误类型定义模块。

本模块定义了 API 调用过程中可能产生的各类错误的异常层级结构。
所有 API 相关的错误均继承自 :class:`OpenHarnessApiError` 基类，
便于上层统一捕获和处理。具体错误类型包括：

- :class:`AuthenticationFailure` — 认证失败（凭据被拒绝）。
- :class:`RateLimitFailure` — 请求频率超限。
- :class:`RequestFailure` — 通用的请求或传输失败。
"""

from __future__ import annotations


class OpenHarnessApiError(RuntimeError):
    """上游 API 失败的基类异常。

    所有 OpenHarness API 调用中产生的业务级错误均继承此类，
    便于上层代码通过 ``except OpenHarnessApiError`` 统一捕获。
    """


class AuthenticationFailure(OpenHarnessApiError):
    """当上游服务拒绝提供的凭据时抛出。

    典型场景包括：API Key 无效、OAuth 令牌过期、权限不足等。
    认证错误通常不应自动重试，需要用户重新配置凭据。
    """


class RateLimitFailure(OpenHarnessApiError):
    """当上游服务因请求频率超限而拒绝请求时抛出。

    触发原因通常是短时间内发送了过多请求。
    某些客户端的自动重试逻辑会将此类错误视为可重试。
    """


class RequestFailure(OpenHarnessApiError):
    """通用的请求或传输失败异常。

    用于表示不属于认证或频率限制的其他 API 请求失败，
    如网络超时、服务端内部错误、响应格式异常等。
    """
