"""网页搜索工具。

本模块提供 WebSearchTool，用于执行网页搜索并返回紧凑的搜索结果。
默认使用 DuckDuckGo HTML 搜索接口，也支持自定义搜索后端。
返回结果包含标题、URL 和摘要片段。
通过解析 HTML 页面中的搜索结果锚点和摘要元素提取信息。
该工具为只读工具。
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.network_guard import NetworkGuardError, fetch_public_http_response


class WebSearchToolInput(BaseModel):
    """网页搜索工具的输入参数。

    Attributes:
        query: 搜索查询词
        max_results: 最大结果数，范围 1-10，默认 5
        search_url: 可选的搜索端点 URL 覆盖
    """

    query: str = Field(description="Search query")
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum number of results")
    search_url: str | None = Field(
        default=None,
        description="Optional override for the HTML search endpoint, useful for private search backends or testing.",
    )


class WebSearchTool(BaseTool):
    """执行网页搜索并返回紧凑搜索结果的工具。

    默认使用 DuckDuckGo HTML 搜索接口，支持自定义搜索后端。
    """

    name = "web_search"
    description = "Search the web and return compact top results with titles, URLs, and snippets."
    input_model = WebSearchToolInput

    def is_read_only(self, arguments: WebSearchToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(
        self,
        arguments: WebSearchToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行网页搜索。

        向搜索端点发送查询请求，解析 HTML 响应中的搜索结果。

        Args:
            arguments: 包含查询词和结果限制的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            格式化的搜索结果（编号、标题、URL、摘要）
        """
        del context
        endpoint = arguments.search_url or "https://html.duckduckgo.com/html/"
        try:
            response = await fetch_public_http_response(
                endpoint,
                params={"q": arguments.query},
                headers={"User-Agent": "OpenHarness/0.1"},
                timeout=20.0,
            )
            response.raise_for_status()
        except (httpx.HTTPError, NetworkGuardError) as exc:
            return ToolResult(output=f"web_search failed: {exc}", is_error=True)

        results = _parse_search_results(response.text, limit=arguments.max_results)
        if not results:
            return ToolResult(output="No search results found.", is_error=True)

        lines = [f"Search results for: {arguments.query}"]
        for index, result in enumerate(results, start=1):
            lines.append(f"{index}. {result['title']}")
            lines.append(f"   URL: {result['url']}")
            if result["snippet"]:
                lines.append(f"   {result['snippet']}")
        return ToolResult(output="\n".join(lines))


def _parse_search_results(body: str, *, limit: int) -> list[dict[str, str]]:
    """解析搜索结果 HTML 页面。

    从 HTML 中提取搜索结果的锚点（标题和链接）和摘要片段，
    返回包含 title、url、snippet 的字典列表。

    Args:
        body: 搜索结果 HTML 页面内容
        limit: 最大结果数

    Returns:
        搜索结果字典列表
    """
    snippets = [
        _clean_html(match.group("snippet"))
        for match in re.finditer(
            r'<(?:a|div|span)[^>]+class="[^"]*(?:result__snippet|result-snippet)[^"]*"[^>]*>(?P<snippet>.*?)</(?:a|div|span)>',
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
    ]

    results: list[dict[str, str]] = []
    anchor_matches = re.finditer(
        r"<a(?P<attrs>[^>]+)>(?P<title>.*?)</a>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for index, match in enumerate(anchor_matches):
        attrs = match.group("attrs")
        class_match = re.search(r'class="(?P<class>[^"]+)"', attrs, flags=re.IGNORECASE)
        if class_match is None:
            continue
        class_names = class_match.group("class")
        if "result__a" not in class_names and "result-link" not in class_names:
            continue
        href_match = re.search(r'href="(?P<href>[^"]+)"', attrs, flags=re.IGNORECASE)
        if href_match is None:
            continue
        title = _clean_html(match.group("title"))
        url = _normalize_result_url(href_match.group("href"))
        snippet = snippets[index] if index < len(snippets) else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _normalize_result_url(raw_url: str) -> str:
    """规范化搜索结果 URL。

    对于 DuckDuckGo 的重定向 URL（/l/ 路径），提取实际目标 URL；
    其他 URL 原样返回。

    Args:
        raw_url: 原始 URL 字符串

    Returns:
        规范化后的 URL
    """
    parsed = urlparse(raw_url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else raw_url
    return raw_url


def _clean_html(fragment: str) -> str:
    """清洗 HTML 片段为纯文本。

    去除 HTML 标签、解码 HTML 实体、压缩空白字符。

    Args:
        fragment: HTML 片段字符串

    Returns:
        清洗后的纯文本
    """
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
