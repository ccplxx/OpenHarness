"""简易 Token 估算工具模块。

本模块提供基于字符启发式规则的 Token 数量估算函数，无需依赖
分词器即可快速给出近似 Token 数量，适用于上下文窗口管理和
对话压缩决策。
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """基于字符启发式规则估算纯文本的 Token 数量。

    使用约 4 字符 = 1 Token 的粗略比例进行估算，空字符串返回 0，
    非空字符串至少返回 1。

    Args:
        text: 待估算的文本字符串。

    Returns:
        int: 估算的 Token 数量。
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_message_tokens(messages: list[str]) -> int:
    """估算一组消息字符串的总 Token 数量。

    对每条消息分别调用 estimate_tokens 后求和。

    Args:
        messages: 消息字符串列表。

    Returns:
        int: 所有消息的 Token 总量估算值。
    """
    return sum(estimate_tokens(message) for message in messages)
