import re
from urllib.parse import parse_qs, urlparse


def format_number(num: int) -> str:
    """格式化数字，大数字使用万/亿单位"""
    if num >= 100000000:
        return f"{num / 100000000:.1f}亿"
    if num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


def format_duration(seconds: int) -> str:
    """格式化时间长度为 HH:MM:SS 或 MM:SS 格式"""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if hours > 0
        else f"{minutes:02d}:{seconds:02d}"
    )


def extract_url_from_text(text: str) -> str | None:
    """从文本中提取第一个URL"""
    url_pattern = re.compile(
        r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.-]*(?:\?[/\w\.-=%&+]*)?"
    )
    match = url_pattern.search(text)
    if match:
        return match.group(0)
    return None


def extract_video_page_num(url: str) -> int:
    """从视频URL中提取分P参数（0基索引）

    如 https://www.bilibili.com/video/BV1xx?p=2 返回 1
    """
    try:
        parsed = urlparse(url)
        if parsed.query:
            query_params = parse_qs(parsed.query)
            if p_value := query_params.get("p"):
                if p_value[0].isdigit():
                    return int(p_value[0]) - 1
    except Exception:
        pass
    return 0
