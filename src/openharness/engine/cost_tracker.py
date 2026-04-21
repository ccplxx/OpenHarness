"""用量（token）聚合追踪器。

本模块提供 CostTracker 类，用于在会话的整个生命周期内累计 API 调用的
输入/输出 token 用量，为费用估算与上下文窗口管理提供数据基础。
"""

from __future__ import annotations

from openharness.api.usage import UsageSnapshot


class CostTracker:
    """用量聚合追踪器。

    在会话的整个生命周期内持续累积 API 调用的 token 用量（input_tokens / output_tokens），
    每次模型响应后将 UsageSnapshot 叠加到运行总计中，供上层查询总消耗。
    """

    def __init__(self) -> None:
        self._usage = UsageSnapshot()

    def add(self, usage: UsageSnapshot) -> None:
        """将一次 API 调用的用量快照叠加到运行总计中。"""
        self._usage = UsageSnapshot(
            input_tokens=self._usage.input_tokens + usage.input_tokens,
            output_tokens=self._usage.output_tokens + usage.output_tokens,
        )

    @property
    def total(self) -> UsageSnapshot:
        """返回截至目前累计的用量快照。"""
        return self._usage
