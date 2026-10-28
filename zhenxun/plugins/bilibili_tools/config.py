import asyncio
import json
import time

try:
    from bilibili_api import Credential
except ImportError:
    raise ImportError(
        "错误：无法导入 bilibili_api 模块。\n"
        "请确保已安装 bilibili-api-python 包，而不是 bilibili-api。\n"
        "请使用以下命令安装：pip install bilibili-api-python"
    )
from pydantic import BaseModel, Field

from zhenxun.configs.config import Config
from zhenxun.configs.path_config import DATA_PATH, TEMP_PATH
from zhenxun.services.log import logger


class GroupSettings(BaseModel):
    """B站工具插件分群配置"""

    auto_download_enabled: bool = Field(
        False, description="是否为当前群组开启B站视频自动下载"
    )


def cookies_str_to_dict(cookies_str: str) -> dict[str, str]:
    """将cookies字符串转换为字典"""
    cookies = {}
    if not cookies_str:
        return cookies
    try:
        items = cookies_str.split(";")
        for item in items:
            if "=" not in item:
                continue
            item = item.strip()
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
    except Exception:
        pass
    return cookies


MODULE_NAME = "bilibili_tools"
base_config = Config.get(MODULE_NAME)

bili_credential: Credential | None = None
_credential_lock = asyncio.Lock()
_credential_loaded = False
_last_refresh_check_time = 0
_REFRESH_CHECK_INTERVAL = 24 * 60 * 60

# 凭证缓存文件：不再写入 data/config.yaml，而是单独存放在一个缓存文件中
CREDENTIAL_FILE = DATA_PATH / MODULE_NAME / "credential.json"


async def load_credential_from_file():
    """从单独的缓存文件加载 Credential

    若缓存文件不存在但旧的配置文件 (data/config.yaml 的 BiliBili.COOKIES) 中存有凭证，
    则自动迁移到新的缓存文件中，避免已登录用户需要重新登录。
    """
    global bili_credential, _credential_loaded
    async with _credential_lock:
        if _credential_loaded:
            return

        try:
            if CREDENTIAL_FILE.exists():
                data = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
                if data:
                    bili_credential = Credential(
                        sessdata=data.get("sessdata"),
                        bili_jct=data.get("bili_jct"),
                        buvid3=data.get("buvid3"),
                        buvid4=data.get("buvid4"),
                        dedeuserid=data.get("dedeuserid"),
                        ac_time_value=data.get("ac_time_value"),
                    )
                    logger.info("成功从缓存文件加载 B站 Credential")
                else:
                    logger.info("缓存文件中的凭证为空")
                    bili_credential = None
            else:
                # 缓存文件不存在，尝试从旧配置 (BiliBili.COOKIES) 迁移凭证
                legacy_cookies = Config.get("BiliBili").get("COOKIES", "")
                if legacy_cookies:
                    logger.info("未找到凭证缓存文件，检测到旧配置中的凭证，开始迁移...")
                    cookies_dict = cookies_str_to_dict(legacy_cookies)
                    bili_credential = Credential(
                        sessdata=cookies_dict.get("SESSDATA"),
                        bili_jct=cookies_dict.get("bili_jct"),
                        buvid3=cookies_dict.get("buvid3"),
                        buvid4=cookies_dict.get("buvid4"),
                        dedeuserid=cookies_dict.get("DedeUserID"),
                        ac_time_value=cookies_dict.get("ac_time_value"),
                    )
                    # 将迁移来的凭证写入新的缓存文件
                    data = {
                        "sessdata": bili_credential.sessdata,
                        "bili_jct": bili_credential.bili_jct,
                        "buvid3": bili_credential.buvid3,
                        "buvid4": bili_credential.buvid4,
                        "dedeuserid": bili_credential.dedeuserid,
                        "ac_time_value": bili_credential.ac_time_value,
                    }
                    CREDENTIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
                    CREDENTIAL_FILE.write_text(
                        json.dumps(data, ensure_ascii=False), encoding="utf-8"
                    )
                    # 迁移完成后清理旧配置，避免退出登录后重启又自动迁移回来
                    Config.set_config("BiliBili", "COOKIES", "", auto_save=True)
                    logger.info("已将旧配置中的凭证迁移到缓存文件，并清理旧配置")
                else:
                    logger.info("B站凭证缓存文件不存在，且旧配置中无凭证")
                    bili_credential = None
        except Exception as e:
            logger.error("加载 Credential 失败", e=e)
            bili_credential = None

        _credential_loaded = True


async def save_credential_to_file(credential: Credential):
    """将 Credential 保存到单独的缓存文件"""
    global bili_credential
    async with _credential_lock:
        try:
            data = {
                "sessdata": credential.sessdata,
                "bili_jct": credential.bili_jct,
                "buvid3": credential.buvid3,
                "buvid4": credential.buvid4,
                "dedeuserid": credential.dedeuserid,
                "ac_time_value": credential.ac_time_value,
            }
            CREDENTIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            CREDENTIAL_FILE.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )

            bili_credential = credential
            logger.info("全局 bili_credential 已更新并保存到缓存文件")
        except Exception as e:
            logger.error("保存 Credential 失败", e=e)


async def clear_credential():
    """清除当前的 Credential 并删除缓存文件"""
    global bili_credential
    async with _credential_lock:
        try:
            if CREDENTIAL_FILE.exists():
                CREDENTIAL_FILE.unlink()
            # 同时清理旧配置中的凭证，避免重启后自动迁移回来
            Config.set_config("BiliBili", "COOKIES", "", auto_save=True)
            bili_credential = None
            logger.info("已清除 bili_credential 并删除缓存文件")
        except Exception as e:
            logger.error("清除 Credential 失败", e=e)


def get_credential() -> Credential | None:
    """获取当前的全局 Credential 对象"""
    return bili_credential


async def check_and_refresh_credential():
    """检查并刷新凭证（如果需要）"""
    global _last_refresh_check_time, bili_credential

    current_time = time.time()
    # 快速路径：未到检查间隔直接返回（无锁）
    if current_time - _last_refresh_check_time < _REFRESH_CHECK_INTERVAL:
        return

    # 双重检查锁定：在锁内重新检查时间并立即占位，确保只有一个协程执行刷新
    async with _credential_lock:
        current_time = time.time()
        if current_time - _last_refresh_check_time < _REFRESH_CHECK_INTERVAL:
            return
        # 获取凭证快照引用，网络请求在锁外执行以不阻塞其他协程
        cred_snapshot = bili_credential
        if not cred_snapshot or not cred_snapshot.has_ac_time_value():
            logger.debug("凭证不存在或没有 ac_time_value，无法刷新")
            # 无凭证时更新检查时间，避免每次调用都进入此分支
            _last_refresh_check_time = current_time
            return
        # 立即更新检查时间戳以"占位"，防止其他协程重复进入刷新流程
        _last_refresh_check_time = current_time

    try:
        need_refresh = await cred_snapshot.check_refresh()
        if need_refresh:
            logger.info("B站凭证需要刷新，正在刷新...")
            await cred_snapshot.refresh()
            # 刷新完成后检查凭证是否在期间被清除（如用户执行了 logout）
            if bili_credential is cred_snapshot:
                await save_credential_to_file(cred_snapshot)
                logger.info("B站凭证刷新成功")
            else:
                logger.info("B站凭证在刷新期间已被清除，跳过保存")
        else:
            logger.debug("B站凭证不需要刷新")
    except Exception as e:
        logger.error("检查或刷新凭证时出错", e=e)
        # 刷新失败时设置较短的退避时间（5分钟），允许重试同时防止频繁请求
        _last_refresh_check_time = current_time - _REFRESH_CHECK_INTERVAL + 300
        logger.warning("凭证刷新失败，将在5分钟后重试")


# 缓存目录
PLUGIN_CACHE_DIR = DATA_PATH / "cache" / MODULE_NAME
PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)

AVATAR_CACHE_DIR = PLUGIN_CACHE_DIR / "avatars"
AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_CACHE_DIR = PLUGIN_CACHE_DIR / "image"
IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

PLUGIN_TEMP_DIR = TEMP_PATH / MODULE_NAME
PLUGIN_TEMP_DIR.mkdir(parents=True, exist_ok=True)

DYNAMIC_PATH = DATA_PATH / MODULE_NAME / "dynamic"
DYNAMIC_PATH.mkdir(exist_ok=True, parents=True)

# 截图相关配置
# 注意：新版 /opus/ 页面在 .opus-detail 和 .bili-opus-view 之间新增了
# .bili-opus-view-wrap 包裹层（承载左侧目录面板），旧的直接子级路径
# "#app > div.opus-detail > div.bili-opus-view" 已失效，改用后代选择器
SCREENSHOT_ELEMENT_OPUS_ALTERNATIVES = [
    ".bili-opus-view",
    ".bili-dyn-item__main",
]
SCREENSHOT_TIMEOUT = 60

# 下载相关配置
SEND_VIDEO_MAX_RETRIES = 3
SEND_VIDEO_RETRY_DELAY = 5
SEND_VIDEO_TIMEOUT = 120

MAX_CONCURRENT_DOWNLOADS = 1

# 订阅推送相关配置
# 单条通知连续推送失败达到该次数后放弃重试并跳过该内容，
# 避免某条内容持续失败时时间戳永不推进、已推送的动态被反复重推
PUSH_MAX_ATTEMPTS = 3
# 推送降级重试之间的等待时间（秒）
PUSH_RETRY_DELAY = 3
# 因图片过大上传失败时，压缩重试要把图片压到的目标体积（MB）
SEND_IMAGE_MAX_SIZE_MB = 2
