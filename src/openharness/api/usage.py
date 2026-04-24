"""LLM 令牌用量追踪模型。

本模块定义了 :class:`UsageSnapshot` 数据模型，用于记录和统计 LLM API
调用的输入/输出令牌用量。该模型被所有 API 客户端在流式响应完成时生成，
供成本追踪、用量统计和用户展示使用。
"""

from __future__ import annotations

from pydantic import BaseModel


class UsageSnapshot(BaseModel):
    """LLM 模型提供的令牌用量快照。

    记录单次 API 调用消耗的输入和输出令牌数量，
    是 API 响应内容的一部分，用于成本追踪和用量统计。

    Attributes:
        input_tokens: 输入令牌数（包括提示词、系统提示、工具定义等），默认为 0。
        output_tokens: 输出令牌数（模型生成的文本和工具调用），默认为 0。
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """返回输入和输出令牌的总数。

        Returns:
            输入令牌数与输出令牌数之和。
        """
        return self.input_tokens + self.output_tokens
