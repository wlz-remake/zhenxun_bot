BILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def get_bilibili_headers(referer: str = "https://www.bilibili.com") -> dict[str, str]:
    """获取B站请求头

    Args:
        referer: Referer 地址，默认 www.bilibili.com。
                  动态请求可传 "https://t.bilibili.com"。
    """
    headers = {
        "User-Agent": BILI_USER_AGENT,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.bilibili.com",
    }

    return headers


def get_bilibili_video_stream_headers(
    referer: str = "https://www.bilibili.com",
) -> dict[str, str]:
    """获取B站视频流下载专用请求头"""
    headers = {
        "User-Agent": BILI_USER_AGENT,
        "Referer": referer,
    }

    return headers
