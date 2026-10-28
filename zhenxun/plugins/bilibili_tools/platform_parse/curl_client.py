"""curl_cffi 共享客户端(TLS 指纹伪装)

知乎/小黑盒等平台通过 TLS 指纹识别非浏览器流量,
curl_cffi 的 impersonate 可伪装 Chrome 指纹绕过拦截;
未安装 curl_cffi 时回退 httpx(无伪装), 由各解析器自行处理反爬失败提示。
"""

import asyncio
from typing import Any

import httpx

from zhenxun.services.log import logger

try:
    from curl_cffi import requests as curl_requests

    CURL_CFFI_AVAILABLE = True
except ImportError:  # pragma: no cover
    curl_requests = None  # type: ignore[assignment]
    CURL_CFFI_AVAILABLE = False

DEFAULT_IMPERSONATE = "chrome131"


class CurlResponse:
    """统一响应结构(curl_cffi 与 httpx 回退路径共用)"""

    __slots__ = ("content_type", "final_url", "status_code", "text")

    def __init__(
        self, status_code: int, final_url: str, text: str, content_type: str
    ):
        self.status_code = status_code
        self.final_url = final_url
        self.text = text
        self.content_type = content_type


async def request_text(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_data: Any = None,
    cookies: dict[str, str] | None = None,
    impersonate: str | None = None,
    timeout: int = 20,
    allow_redirects: bool = True,
) -> CurlResponse:
    """发起请求并返回文本响应(优先 curl_cffi, 回退 httpx)

    curl_cffi 为同步库, 在线程池中执行避免阻塞事件循环。
    """
    if CURL_CFFI_AVAILABLE:

        def do_curl_request() -> CurlResponse:
            resp = curl_requests.request(  # type: ignore[union-attr]
                method,
                url,
                params=params,
                json=json_data,
                cookies=cookies,
                headers=headers,
                impersonate=impersonate or DEFAULT_IMPERSONATE,
                timeout=timeout,
                allow_redirects=allow_redirects,
            )
            return CurlResponse(
                status_code=int(resp.status_code),
                final_url=str(resp.url),
                text=str(resp.text),
                content_type=str(resp.headers.get("content-type", "")),
            )

        return await asyncio.to_thread(do_curl_request)

    logger.debug(
        "curl_cffi 未安装, 使用 httpx 回退(无 TLS 指纹伪装, 可能被部分平台风控拦截)"
    )
    async with httpx.AsyncClient(
        follow_redirects=allow_redirects, timeout=timeout
    ) as client:
        resp = await client.request(
            method,
            url,
            params=params,
            json=json_data,
            cookies=cookies,
            headers=headers,
        )
        return CurlResponse(
            status_code=resp.status_code,
            final_url=str(resp.url),
            text=resp.text,
            content_type=resp.headers.get("content-type", ""),
        )
