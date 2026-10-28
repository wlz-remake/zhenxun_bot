from pydantic import BaseModel
from tortoise import fields

from zhenxun.services.db_context import Model
from zhenxun.services.log import logger

# ============================================================
# 订阅相关数据库模型 (源自 bilibili_sub)
# ============================================================


class BiliSub(Model):
    id = fields.IntField(pk=True, generated=True, auto_increment=True)
    """数据库自增ID，用于删除和配置"""
    uid = fields.BigIntField(unique=True)
    """B站用户UID"""
    room_id = fields.BigIntField(null=True, unique=True)
    """B站直播间ID"""
    uname = fields.CharField(255, null=True)
    """UP主/主播名称"""

    last_dynamic_timestamp = fields.BigIntField(null=True, default=0)
    """最新动态时间戳"""
    last_video_timestamp = fields.BigIntField(null=True, default=0)
    """最新视频时间戳"""
    live_status = fields.IntField(null=True, default=0)
    """直播状态 0:未开播 1:直播中"""

    push_dynamic = fields.BooleanField(default=True)
    """是否推送动态"""
    push_video = fields.BooleanField(default=True)
    """是否推送视频"""
    push_live = fields.BooleanField(default=True)
    """是否推送直播"""

    at_all_dynamic = fields.BooleanField(default=False)
    """是否在推送动态时@全体"""
    at_all_video = fields.BooleanField(default=False)
    """是否在推送视频时@全体"""
    at_all_live = fields.BooleanField(default=False)
    """是否在推送直播时@全体"""

    class Meta:  # pyright: ignore [reportIncompatibleVariableOverride]
        table = "bilisub"
        table_description = "B站订阅信息表"


class BiliSubTarget(Model):
    id = fields.IntField(pk=True, generated=True, auto_increment=True)
    """自增ID"""
    subscription = fields.ForeignKeyField("models.BiliSub", related_name="targets")
    """关联的订阅信息"""
    target_id = fields.CharField(255)
    """订阅目标ID (e.g., 'group_123456', 'private_789012')"""

    class Meta:  # pyright: ignore [reportIncompatibleVariableOverride]
        table = "bilisub_target"
        table_description = "B站订阅关系表"
        unique_together = ("subscription", "target_id")

    @classmethod
    async def clean_orphaned_subs(cls):
        """清理没有任何目标订阅的BiliSub记录"""
        all_sub_ids = await BiliSub.all().values_list("id", flat=True)
        active_sub_ids = (
            await cls.all().distinct().values_list("subscription_id", flat=True)
        )
        orphaned_ids = set(all_sub_ids) - set(active_sub_ids)
        if orphaned_ids:
            deleted_count = await BiliSub.filter(id__in=list(orphaned_ids)).delete()
            logger.info(f"清理了 {deleted_count} 个孤立的B站订阅记录。")


# ============================================================
# 解析相关数据模型 (源自 parse_bilibili)
# ============================================================


class Owner(BaseModel):
    mid: int
    name: str
    face: str


class Stat(BaseModel):
    aid: int
    view: int
    danmaku: int
    reply: int
    favorite: int
    coin: int
    share: int
    now_rank: int
    his_rank: int
    like: int
    dislike: int
    vt: int | None = None
    vv: int | None = None


class VideoInfo(BaseModel):
    bvid: str
    aid: int
    videos: int
    tid: int
    tname: str
    copyright: int
    pic: str
    title: str
    pubdate: int
    ctime: int
    desc: str
    state: int
    duration: int
    mission_id: int | None = None
    rights: dict
    owner: Owner
    stat: Stat
    dynamic: str
    cid: int
    dimension: dict
    short_link_v2: str | None = None
    up_from_v2: int | None = None
    first_frame: str | None = None
    pub_location: str | None = None
    pages: list | None = None
    parsed_url: str
    ai_summary: str | None = None
    online_count: str | None = None


class LiveInfo(BaseModel):
    room_id: int
    short_id: int
    uid: int
    title: str
    cover: str
    live_status: int
    live_start_time: int
    area_id: int
    area_name: str
    parent_area_id: int
    parent_area_name: str
    description: str
    keyframe_url: str | None = None
    parsed_url: str
    uname: str | None = None
    face: str | None = None
    room_url: str | None = None
    space_url: str | None = None


class ArticleInfo(BaseModel):
    id: str
    type: str
    url: str
    title: str | None = None
    author: str | None = None
    screenshot_path: str | None = None
    screenshot_bytes: bytes | None = None
    markdown_content: str | None = None
    images: list[str] | None = None


class UserStat(BaseModel):
    """用户统计信息"""

    following: int = 0
    follower: int = 0
    archive_view: int = 0
    article_view: int = 0
    likes: int = 0


class UserInfo(BaseModel):
    """用户信息"""

    mid: int
    name: str
    face: str
    sign: str = ""
    level: int = 0
    sex: str = "保密"
    birthday: str = ""
    top_photo: str = ""
    live_room_status: int = 0
    live_room_url: str = ""
    live_room_title: str = ""
    stat: UserStat
    parsed_url: str
