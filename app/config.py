#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hailuo-service 配置。

沿用 jimeng 的两条纪律（它们挡住过真问题）：

  1. **每个旋钮都必须有人读** —— 没人读的配置项就是假配置，
     会让运维以为"我调过了"。`tests/test_config.py` 逐条断言。
  2. **默认值必须能说出依据** —— 要么实测值、要么刻意的策略选择；
     拿不出依据的宁可启动即报错。

刻意不用 pydantic-settings：stdlib 解析更容易审计，依赖面更小。
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field

_TRUE = {"1", "true", "yes", "on", "y", "t"}


class ConfigError(RuntimeError):
    """配置本身有问题 —— **启动即失败**，绝不静默退回默认值。

    静默降级是最坏的失败：它会让"任务不丢""鉴权开着"这类承诺在没人注意时失效。
    """


def _s(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return default if v is None else v.strip()


def _i(key: str, default: int) -> int:
    raw = _s(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} 必须是整数，实得 {raw!r}") from e


def _f(key: str, default: float) -> float:
    raw = _s(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{key} 必须是数字，实得 {raw!r}") from e


def _b(key: str, default: bool) -> bool:
    raw = _s(key)
    if not raw:
        return default
    return raw.lower() in _TRUE


def _csv(key: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in _s(key).split(",") if p.strip())


@dataclass
class Settings:
    # ------------------------------------------------------------ 上游凭据
    #: hailuo 的**唯一**硬前提凭据：浏览器 localStorage 里的 JWT，随 `token` 头发送。
    #: 取法：登录 hailuoai.video → DevTools → Application → Local Storage → 找 JWT。
    #: **运维凭据（可选）**：脚本、E2E、嵌入模式用；服务本身**不再需要**它 ——
    #: 服务端鉴权是透传（请求自带 hailuo JWT）。留它是为了让脚本能
    #: `register_credential()` 后直接跑，无需额外配置。
    hailuo_token: str = ""
    hailuo_base_url: str = "https://hailuoai.video"

    # ------------------------------------------------------------ 设备指纹
    #: 以下 6 个值**参与 `yy` 签名**（见 `upstream/hailuo/sign.py`）。
    #: 上游**不校验其真实性**（实测改 screen_* 仍 200），但要保持**同一会话内稳定**，
    #: 否则同一账号短时间内从多个"设备"发起会被风控关注。
    #: 默认值 = 抓包环境（macOS / Chrome / 2560×1440）。
    hailuo_uuid: str = "00000000-0000-4000-8000-000000000000"
    hailuo_device_id: str = "100000000000000000"
    hailuo_lang: str = "zh-Intl"
    hailuo_os_name: str = "Mac"
    hailuo_browser_name: str = "chrome"
    hailuo_device_memory: int = 32
    hailuo_cpu_core_num: int = 10
    hailuo_browser_language: str = "zh-CN"
    hailuo_browser_platform: str = "MacIntel"
    hailuo_screen_width: int = 2560
    hailuo_screen_height: int = 1440

    # ------------------------------------------------------------ 对外鉴权
    #: 空 = **关闭鉴权**（仅限内网/联调；启动打 WARNING 兜底）。

    # ------------------------------------------------------------ 节奏闸门
    #: 同时在上游跑的任务数 —— **每账号**（号池模式：总并发 = N 账号 × 此值）。
    #: 默认 1 = 策略选择（hailuo 是**计费**上游）。
    hl_concurrency: int = 1
    #: 相邻两次**建任务**提交之间的最小间隔（秒）—— 按账号生效。0 = 不限。
    hl_min_interval: float = 0.0
    #: 每分钟最多建几个任务 —— 按账号生效。0 = 不限。
    hl_per_minute: int = 0
    #: 命中风控后的冷却时长（秒）。冷却期内**该账号**不建任务
    #:（其他账号的队列不受影响；在途任务继续轮询）。
    hl_cooldown: float = 600.0
    #: 同步接口 `POST /v1/images/generations`（创建+轮询合并）的**总等待预算**（秒）：
    #: 预算内到终态直接给结果；耗尽仍在跑 ⇒ 202 + task_id 无缝转异步轮询
    #:（任务不取消，协调器继续推进）。图片实测最慢档 ~124s ⇒ 300s 足够覆盖。
    sync_max_wait: float = 300.0
    #: 同步端点的**并发等待上限**（信号量，进程内）。同步等待阻塞线程池线程
    #:（anyio 默认 40）⇒ 用硬护栏把最坏占用钉死，槽满 **429 秒回**（带 Retry-After），
    #: 绝不让慢请求把线程池吃光而殃及其他请求。8 = 常规号池场景绰绰有余。
    sync_max_concurrency: int = 8
    #: 建任务的**提交重试上限**（仅用于"建任务之前"的瞬时失败：输入图下载/上传、
    #: 连接被掐）。这类失败**上游一定没有建任务** ⇒ 重试不会重复计费。
    #: 3 = 实测连接抖动在 1~2 次内收敛；超过 3 次说明不是抖动，判失败更诚实。
    submit_max_attempts: int = 3

    # ------------------------------------------------------------ 轮询
    #: 一轮 tick 里，同一批任务合并成**一次**上游查询（hailuo 的 batch 端点天然支持）。
    #: 默认 5s：hailuo 出图实测 3~20s，5s 足以在成图后一轮内拿到，
    #: 又不至于把上游查询打爆（2026-09-21 实测 batch 列表接口无频控迹象）。
    hailuo_poll_interval: float = 5.0
    #: 建任务之后、**第一次轮询之前**的等待（秒）。
    #: 取 0.5：早问一次是**零代价**的（未落库的 id 只是"还没好"），晚问才是代价。
    poll_grace: float = 0.5
    #: 🔴 **唯一的任务超时旋钮**（曾经同时有 `HL_MAX_WAIT` 与 `TASK_TIMEOUT`
    #: 两个旋钮指同一件事 —— 改了一个另一个不生效，是典型的"改了没效果"来源）。
    task_timeout: float = 900.0
    #: **视频**任务单独一个超时：视频生成比出图慢一个量级（实测 veo/1080p 分钟级），
    #: 用图片的 900s 会把还在跑的任务判死 —— 而上游仍在计费。
    video_task_timeout: float = 3600.0
    #: 视频轮询间隔（秒）。视频一轮只打 **1 次** v4 点名查询，10s 足够；
    #: 太密只会白问（视频生成是分钟级的）。
    video_poll_interval: float = 10.0
    #: 列表端点 `GET /api/v3/contents/generations/tasks` 是否**要求**凭据。
    #: 🔴 默认 **1**。它会列出任务 id，而 id 就是读接口的凭据 ⇒ 无凭据开放
    #: 等于把凭据派发出去。确要开放请显式设 0（带凭据时仍只列自己的）。
    video_list_require_auth: bool = True

    # ------------------------------------------------------------ 协调器
    coordinator_enabled: bool = True
    coordinator_tick: float = 1.0
    #: 数据库租约。必须 >= 2×轮询间隔，否则每轮都换主 = 没有选主。
    coordinator_lease: float = 30.0

    # ------------------------------------------------------------ 持久化
    #: 任务库。生产 PostgreSQL；SQLite 留给测试与联调（默认即 SQLite，开箱能跑）。
    task_db: str = "sqlite+pysqlite:///./hailuo.db"
    task_db_pool_size: int = 5
    task_db_max_overflow: int = 10
    task_db_pool_recycle: int = 1800
    task_db_pool_pre_ping: bool = True
    task_db_connect_timeout: int = 10
    task_retention_days: int = 7

    # ------------------------------------------------------------ 输入图
    #: 下载输入图的体积上限。**这就是"输入图多大"的唯一旋钮** ——
    #: 再加一个 `MAX_INPUT_BYTES` 只会制造两个都"看起来该改"的旋钮。
    max_download_bytes: int = 32 * 1024 * 1024
    #: 上传前归一化（缩放 / 转 JPEG）。hailuo 的 `maxSupportImageCount` 与体积上限
    #: 未逐一取证 ⇒ 只做保守压缩，不猜阈值。
    normalize_uploads: bool = True
    normalize_max_side: int = 4096
    normalize_max_bytes: int = 4 * 1024 * 1024
    #: 上传结果缓存 TTL（秒）。上游是否回收未引用素材**未知** ⇒ 取保守的 6h。
    upload_cache_ttl: float = 6 * 3600.0
    #: 多张输入图的 **ingest 并行度**（下载→归一化→上传，每张图一条独立流水线）。
    #: 4 = 保守值：实测三参考图串行 ingest ~20s（每张 5~8s），并行后 ≈ 最慢一张；
    #: 上限受线程池钳制，不会随张数涨。1 = 退回串行（排障用）。
    ingest_parallelism: int = 4

    # ------------------------------------------------------------ 超分（腾讯 CI）
    #: 🔴 成本策略第二段（2026-10-15 用户口径）：gpt-image-* 上游**恒按最便宜档**
    #: 生成（1K+low），调用方要的更高档位由腾讯云数据万象（CI）超分补齐。
    #: 凭据一行式 `SECRET_ID|SECRET_KEY`（与 bfl 项目的 TENCENT_API_KEY 同格式，
    #: 也可分写 TENCENT_SECRET_ID / TENCENT_SECRET_KEY）。
    tencent_api_key: str = ""
    #: 超分产物所在的 COS 桶（CI 处理直接作用在桶对象上）。
    superres_bucket: str = ""
    superres_region: str = "ap-singapore"
    #: 总开关。**默认开**但未配凭据 ⇒ 自动回退原图并留痕（不会静默）。
    superres_enabled: bool = True
    #: CI 同步请求超时（秒）。bfl 实测 512²→1024² 秒级；给足余量。
    superres_timeout: float = 90.0
    #: 预签名 URL 有效期（秒）。24h：调用方（newapi 号池）拿到手还会转存。
    superres_url_ttl: float = 86400.0

    # ------------------------------------------------------------ 负缓存（内容违规）
    #: 🔴 同一 (prompt, 输入图) 组合被上游判违规后，TTL 内直接 451 拒绝、
    #: **不再打上游**——重复提交违禁内容只会重复浪费审核往返（且上游是否
    #: 对被拒请求计费未取证）。内存态：单容器单进程足够，重启清空可接受。
    negcache_enabled: bool = True
    negcache_ttl: float = 86400.0

    # ------------------------------------------------------------ 可观测性
    otel_token: str = ""
    otel_service_name: str = "hailuo-service"
    otel_environment: str = ""
    #: 1 = 把上游原始报文绑成 span 属性（**默认开**：观测面不脱敏，
    #: 事后"顺手脱敏"会改掉上游实际字段名，让人对着面板排查一个不存在的字段）。
    otel_capture_upstream: bool = True
    #: 脱敏开关。**默认 0**。打开只影响 SDK 自带 scrubber；
    #: 本模块自身在任何设置下都不改写上报内容。
    otel_scrubbing: bool = False

    # ------------------------------------------------------------ 服务
    host: str = "0.0.0.0"
    port: int = 8300
    log_level: str = "INFO"

    startup_warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 派生

    @property
    def upstream_configured(self) -> bool:
        """上游凭据是否可用。false 时服务仍能启动（`/healthz` 200），但不受理任务。

        ⚠️ 必须 `.strip()` 后判：直接 `Settings(hailuo_token="   ")`
        （测试或程序化构造，不走 `_s()`）会得到"看起来配了、其实是空白"的状态，
        而它会让 `/readyz` 报 ready 却在建任务时 401。
        """
        return bool((self.hailuo_token or "").strip())

    @property
    def db_target(self) -> str:
        return self.task_db

    @property
    def is_sqlite(self) -> bool:
        return self.task_db.startswith("sqlite")

    def device_profile(self) -> dict[str, object]:
        """签名用的设备指纹。**唯一出口** —— 别在别处拼这两组值。"""
        return {
            "uuid": self.hailuo_uuid,
            "device_id": self.hailuo_device_id,
            "lang": self.hailuo_lang,
            "os_name": self.hailuo_os_name,
            "browser_name": self.hailuo_browser_name,
            "device_memory": self.hailuo_device_memory,
            "cpu_core_num": self.hailuo_cpu_core_num,
            "browser_language": self.hailuo_browser_language,
            "browser_platform": self.hailuo_browser_platform,
            "screen_width": self.hailuo_screen_width,
            "screen_height": self.hailuo_screen_height,
        }

    def replace(self, **kw) -> "Settings":
        """dataclass 没有 pydantic 的 `model_copy()` —— 派生配置统一用它。

        ⚠️ `validate()` **不会自动重跑**：改完校验类字段请自行再调一次。
        """
        return dataclasses.replace(self, **kw)

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_env(cls) -> "Settings":
        st = cls(
            hailuo_token=_s("HAILUO_TOKEN"),
            hailuo_base_url=_s("HAILUO_BASE_URL", "https://hailuoai.video"),
            hailuo_uuid=_s("HAILUO_UUID", "00000000-0000-4000-8000-000000000000"),
            hailuo_device_id=_s("HAILUO_DEVICE_ID", "100000000000000000"),
            hailuo_lang=_s("HAILUO_LANG", "zh-Intl"),
            hailuo_os_name=_s("HAILUO_OS_NAME", "Mac"),
            hailuo_browser_name=_s("HAILUO_BROWSER_NAME", "chrome"),
            hailuo_device_memory=_i("HAILUO_DEVICE_MEMORY", 32),
            hailuo_cpu_core_num=_i("HAILUO_CPU_CORE_NUM", 10),
            hailuo_browser_language=_s("HAILUO_BROWSER_LANGUAGE", "zh-CN"),
            hailuo_browser_platform=_s("HAILUO_BROWSER_PLATFORM", "MacIntel"),
            hailuo_screen_width=_i("HAILUO_SCREEN_WIDTH", 2560),
            hailuo_screen_height=_i("HAILUO_SCREEN_HEIGHT", 1440),

            hl_concurrency=_i("HL_CONCURRENCY", 1),
            hl_min_interval=_f("HL_MIN_INTERVAL", 0.0),
            hl_per_minute=_i("HL_PER_MINUTE", 0),
            hl_cooldown=_f("HL_COOLDOWN", 600.0),
            submit_max_attempts=_i("SUBMIT_MAX_ATTEMPTS", 3),
            sync_max_wait=_f("SYNC_MAX_WAIT", 300.0),
            sync_max_concurrency=_i("SYNC_MAX_CONCURRENCY", 8),
            hailuo_poll_interval=_f("HAILUO_POLL_INTERVAL", 5.0),
            poll_grace=_f("POLL_GRACE", 0.5),
            task_timeout=_f("TASK_TIMEOUT", 900.0),
            video_task_timeout=_f("VIDEO_TASK_TIMEOUT", 3600.0),
            video_poll_interval=_f("VIDEO_POLL_INTERVAL", 10.0),
            video_list_require_auth=_b("VIDEO_LIST_REQUIRE_AUTH", True),
            coordinator_enabled=_b("COORDINATOR_ENABLED", True),
            coordinator_tick=_f("COORDINATOR_TICK", 1.0),
            coordinator_lease=_f("COORDINATOR_LEASE", 30.0),
            task_db=_s("TASK_DB", "sqlite+pysqlite:///./hailuo.db"),
            task_db_pool_size=_i("TASK_DB_POOL_SIZE", 5),
            task_db_max_overflow=_i("TASK_DB_MAX_OVERFLOW", 10),
            task_db_pool_recycle=_i("TASK_DB_POOL_RECYCLE", 1800),
            task_db_pool_pre_ping=_b("TASK_DB_POOL_PRE_PING", True),
            task_db_connect_timeout=_i("TASK_DB_CONNECT_TIMEOUT", 10),
            task_retention_days=_i("TASK_RETENTION_DAYS", 7),
            max_download_bytes=_i("MAX_DOWNLOAD_BYTES", 32 * 1024 * 1024),
            normalize_uploads=_b("NORMALIZE_UPLOADS", True),
            normalize_max_side=_i("NORMALIZE_MAX_SIDE", 4096),
            normalize_max_bytes=_i("NORMALIZE_MAX_BYTES", 4 * 1024 * 1024),
            upload_cache_ttl=_f("UPLOAD_CACHE_TTL", 6 * 3600.0),
            ingest_parallelism=_i("INGEST_PARALLELISM", 4),
            tencent_api_key=(
                _s("TENCENT_API_KEY")
                or (_s("TENCENT_SECRET_ID") + "|" + _s("TENCENT_SECRET_KEY")
                    if _s("TENCENT_SECRET_ID") and _s("TENCENT_SECRET_KEY") else "")
            ),
            superres_bucket=_s("SUPERRES_BUCKET"),
            superres_region=_s("SUPERRES_REGION", "ap-singapore"),
            superres_enabled=_b("SUPERRES_ENABLED", True),
            superres_timeout=_f("SUPERRES_TIMEOUT", 90.0),
            superres_url_ttl=_f("SUPERRES_URL_TTL", 86400.0),
            negcache_enabled=_b("NEG_CACHE_ENABLED", True),
            negcache_ttl=_f("NEG_CACHE_TTL", 86400.0),
            otel_token=_s("LOGFIRE_TOKEN"),
            otel_service_name=_s("OTEL_SERVICE_NAME", "hailuo-service"),
            otel_environment=_s("LOGFIRE_ENVIRONMENT"),
            otel_capture_upstream=_b("OTEL_CAPTURE_UPSTREAM", True),
            otel_scrubbing=_b("OTEL_SCRUBBING", False),
            host=_s("HOST", "0.0.0.0"),
            port=_i("PORT", 8300),
            log_level=_s("LOG_LEVEL", "INFO").upper(),
        )
        st.validate()
        return st

    # ------------------------------------------------------------------ 校验

    def validate(self) -> None:
        if self.hl_concurrency < 1:
            raise ConfigError("HL_CONCURRENCY 必须 >= 1")
        if self.submit_max_attempts < 1:
            raise ConfigError("SUBMIT_MAX_ATTEMPTS 必须 >= 1")
        if self.task_timeout <= 0:
            raise ConfigError("TASK_TIMEOUT 必须 > 0")
        if self.sync_max_wait <= 0:
            raise ConfigError("SYNC_MAX_WAIT 必须 > 0")
        if self.sync_max_concurrency < 1:
            raise ConfigError("SYNC_MAX_CONCURRENCY 必须 >= 1")
        if self.hailuo_poll_interval <= 0:
            raise ConfigError("HAILUO_POLL_INTERVAL 必须 > 0")
        if self.task_retention_days < 1:
            raise ConfigError("TASK_RETENTION_DAYS 必须 >= 1")
        if self.task_db_pool_size < 1:
            raise ConfigError("TASK_DB_POOL_SIZE 必须 >= 1")
        if self.normalize_max_side < 64:
            raise ConfigError("NORMALIZE_MAX_SIDE 太小（<64），会毁图")
        if self.ingest_parallelism < 1:
            raise ConfigError("INGEST_PARALLELISM 必须 >= 1")
        if self.coordinator_lease < self.hailuo_poll_interval * 2:
            raise ConfigError("COORDINATOR_LEASE 必须 >= 2×HAILUO_POLL_INTERVAL")
        if self.video_task_timeout <= 0:
            raise ConfigError("VIDEO_TASK_TIMEOUT 必须 > 0")
        if self.video_poll_interval <= 0:
            raise ConfigError("VIDEO_POLL_INTERVAL 必须 > 0")
        if self.hailuo_max_side_guard():
            raise ConfigError(
                "HAILUO_SCREEN_WIDTH/HEIGHT 必须为正整数（它们参与签名）")

        self.startup_warnings = []
        if not self.upstream_configured:
            self.startup_warnings.append(
                "HAILUO_TOKEN 未配置 —— 服务对外可用（鉴权是透传：请求自带 hailuo JWT），"
                "但 scripts/ 下的运维脚本与嵌入模式需要它。")
        if self.hl_concurrency > 2:
            self.startup_warnings.append(
                f"HL_CONCURRENCY={self.hl_concurrency} 超过本项目实测验证过的上限（2）。"
                "hailuo 是计费上游，并发直接放大额度消耗速率与风控暴露面。")
        if not self.coordinator_enabled:
            self.startup_warnings.append(
                "COORDINATOR_ENABLED=0 —— 任务只会停在 queued，**永远不会建到上游**。"
                "仅用于冒烟/自检。")

    def hailuo_max_side_guard(self) -> bool:
        return self.hailuo_screen_width <= 0 or self.hailuo_screen_height <= 0


__all__ = ["Settings", "ConfigError"]
