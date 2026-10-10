#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力注册表 —— **本服务唯一的能力真相**。

改动这里 = 改动对外能做什么、单价多少、叫什么名字。

## 数据从哪来（两条源，都零成本）

1. **`create_image_models`** —— `GET /public/api/config/web/common_config`（公开、免鉴权）
   ⇒ 模型**清单**、`maxSupportImageCount`（能垫几张图）、`maxPromptLength`、展示名、tags。
2. **`imageModels`** —— `GET /public/v2/api/multimodal/video/model/info`（公开、免鉴权）
   ⇒ 每个模型的 `parameter.resolutions[]` / `parameter.aspectRatios[]`（**含 defaultSelect**）、
   `costs[]`（**按 resolution × quality 的真实单价**）、`defaultCost`。

⇒ 运行期零成本读取（不建任务、不计费），读不到时退回 `FROZEN_SNAPSHOT` **并留降级痕**。

## 为什么把"能垫几张图"写进注册表

`maxSupportImageCount` 是**服务端声明**的（不是经验值）：`gpt-image-*` 是 16、
`nano_banana*` 是 14、`gpt-image-1.5` 只有 3、`image-01` 没给。
本服务**按每个模型自己的上界**校验，超过就 400 —— 而不是拿一个全局经验值糊弄。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import InvalidParameterError

#: 快照日期。**改 `FROZEN_SNAPSHOT` 必须同步改它** —— 它是"数据有多旧"的唯一线索。
SNAPSHOT_DATE = "2026-09-21"


# ---------------------------------------------------------------------------
# 能力（本服务对外宣告的 model 取值）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """一条对外能力：名字 + 它约束什么。"""

    name: str
    upstream_mode: str
    requires_image: bool
    requires_prompt: bool
    aliases: tuple[str, ...] = ()
    notes: str = ""


#: 图片族三兄弟。**`hailuo-i2i` 是本项目的重点**（图生图：垫图 + 指令）。
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        name="hailuo-i2i",
        upstream_mode="image-reference",
        requires_image=True,
        requires_prompt=True,
        aliases=(
            "i2i", "image2image", "img2img", "图生图", "垫图", "参考图",
            "hailuo", "hailuo-image", "image", "edit", "修图", "改图",
        ),
        notes="图生图：1..N 张垫图（N 由所选模型的 maxSupportImageCount 决定）。"
              "本项目重点能力，有端到端抓包证据。",
    ),
    Capability(
        name="hailuo-t2i",
        upstream_mode="image-reference",
        requires_image=False,
        requires_prompt=True,
        aliases=("t2i", "text2image", "txt2img", "文生图", "生图"),
        notes="文生图：fileList 传空数组。与 i2i 走**同一个上游端点**，"
              "差别只在 fileList 是否为空。",
    ),
    Capability(
        name="hailuo-image",
        upstream_mode="image-reference",
        requires_image=False,
        requires_prompt=True,
        aliases=("auto", "hailuo-auto"),
        notes="自动：按 `image` 是否为空推导 i2i / t2i。显式写它等价于不写 model。",
    ),
)

#: 第三方 SDK 常硬编码的占位名 ⇒ 等价于"没写 model"，走默认推导。
PLACEHOLDER_MODELS: frozenset[str] = frozenset({
    "auto", "dall-e-3", "dall-e-2", "gpt-image-1", "gpt-image", "flux", "sdxl",
    "stable-diffusion", "seedream", "hailuo-video", "",
})

#: 默认模型。**依据**：本项目唯一有端到端抓包证据的图生图模型
#: （2026-09-21 抓包：1 张参考图 + `desc="a cat"` → 4096×4096 PNG，1K/2K/4K 分别 4/5/8 积分）。
#: ⚠️ 它不是**最便宜**的（`image-01` 固定 1 积分），但它是**唯一实测跑通**的。
#: 要更便宜请显式传 `model`。
DEFAULT_MODEL = "nano_banana21_flash"


# ---------------------------------------------------------------------------
# 上游模型（冻结快照）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamModel:
    """一个上游图片模型。字段全部来自服务端，无一是经验值。"""

    model_id: str
    display_name: str
    #: `create_image_models.models[].modelList[].maxSupportImageCount`
    max_support_image_count: int | None
    max_prompt_length: int | None
    #: `imageModels[].parameter.resolutions[].value`
    resolutions: tuple[str, ...]
    #: `defaultSelect=true` 的 resolution
    default_resolutions: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    default_aspect_ratios: tuple[str, ...]
    #: `imageModels[].costs[]` —— 原样保留（resolution × quality → realCost）
    costs: tuple[dict[str, Any], ...] = ()
    default_cost: float | None = None
    tags: tuple[str, ...] = ()
    #: 上游声明的 mode（`image-reference` / `subject-reference`）
    mode: str = "image-reference"

    def cost_for(self, resolution: str, quality: str | None = None) -> float | None:
        """查该 (resolution, quality) 的真实单价。查不到返回 `None`（**不猜**）。

        计价表里有两种形态：
          · 带 `qualities`（`gpt-image-*`）：必须同时命中 resolution 与 quality；
          · 不带 `qualities`（`nano_banana*` / `seedream-*` 等）：只按 resolution。
        """
        for row in self.costs:
            if resolution not in (row.get("resolutions") or []):
                continue
            quals = row.get("qualities") or []
            if not quals:
                if not quality:
                    return float(row.get("realCost") or 0)
                continue
            if quality and quality in quals:
                return float(row.get("realCost") or 0)
        # 纯 quality 型模型（gpt-image-1.5）：resolutions 里装的是 Low/Medium/High
        if quality:
            for row in self.costs:
                if quality in (row.get("resolutions") or []):
                    return float(row.get("realCost") or 0)
        return None

    def supports_resolution(self, resolution: str) -> bool:
        return resolution in self.resolutions

    def supports_aspect_ratio(self, ratio: str) -> bool:
        return ratio in self.aspect_ratios

    def to_public(self) -> dict[str, Any]:
        """`/async/v1/models` 的一项（OpenAI 形态 + `hailuo` 扩展块）。"""
        out: dict[str, Any] = {
            "id": self.model_id,
            "object": "model",
            "owned_by": "hailuo",
            "name": self.display_name,
            "max_images": self.max_support_image_count,
            "max_prompt_length": self.max_prompt_length,
            "resolutions": list(self.resolutions),
            "aspect_ratios": list(self.aspect_ratios),
        }
        if self.default_cost is not None:
            out["default_cost"] = self.default_cost
        if self.tags:
            out["tags"] = list(self.tags)
        return out


def _row(res: str | list[str], cost: float, **kw: Any) -> dict[str, Any]:
    resolutions = [res] if isinstance(res, str) else res
    return {"resolutions": resolutions, "realCost": cost, "rawCost": 0, "unitFileCost": 0,
            "qualities": [], **kw}


_AR_ALL = ("Auto", "21:9", "16:9", "5:4", "4:3", "3:2", "1:1", "2:3", "3:4", "4:5", "9:16")

#: 冻结快照：2026-09-21 从两个公开端点读出，**逐字段照抄**，未做任何推断。
#: ⚠️ 它只是"读不到时"的兜底 —— 正常路径一律走运行期实读 + 留痕。
FROZEN_SNAPSHOT: tuple[UpstreamModel, ...] = (
    UpstreamModel(
        model_id="nano_banana21_flash", display_name="Nano Banana 2",
        max_support_image_count=14, max_prompt_length=7500,
        resolutions=("1K", "2K", "4K"), default_resolutions=("1K", "2K", "4K"),
        aspect_ratios=("Auto", "21:9", "16:9", "5:4", "4:3", "3:2", "1:1", "2:3",
                       "3:4", "4:5", "9:16", "1:4", "4:1", "1:8", "8:1"),
        default_aspect_ratios=("Auto",),
        costs=(_row("1K", 4), _row("2K", 5), _row("4K", 8)),
        default_cost=0, tags=("edit", "4k"),
    ),
    UpstreamModel(
        model_id="nano-banana2", display_name="Nano Banana Pro",
        max_support_image_count=14, max_prompt_length=7500,
        resolutions=("1K", "2K", "4K"), default_resolutions=("1K", "2K", "4K"),
        aspect_ratios=_AR_ALL, default_aspect_ratios=("Auto",),
        costs=(_row(["1K", "2K"], 6), _row("4K", 10)),
        default_cost=0, tags=("edit", "4k"),
    ),
    UpstreamModel(
        model_id="seedream-5.0", display_name="Seedream 5.0 Lite",
        max_support_image_count=14, max_prompt_length=None,
        resolutions=("2K", "4K"), default_resolutions=("2K", "4K"),
        aspect_ratios=_AR_ALL, default_aspect_ratios=("Auto",),
        costs=(_row(["2K", "4K"], 4),), default_cost=0, tags=("edit",),
    ),
    UpstreamModel(
        model_id="seedream-4.5", display_name="Seedream 4.5",
        max_support_image_count=14, max_prompt_length=None,
        resolutions=("2K", "4K"), default_resolutions=("2K", "4K"),
        aspect_ratios=_AR_ALL, default_aspect_ratios=("Auto",),
        costs=(_row(["2K", "4K"], 4),), default_cost=0, tags=("edit", "4k"),
    ),
    UpstreamModel(
        model_id="gpt-image-2", display_name="GPT Image 2",
        max_support_image_count=16, max_prompt_length=7500,
        resolutions=("1K", "2K", "4K"), default_resolutions=("1K", "2K", "4K"),
        aspect_ratios=("21:9", "16:9", "5:4", "4:3", "3:2", "1:1", "2:3", "3:4", "4:5", "9:16"),
        default_aspect_ratios=("4:3",),
        costs=(
            _row("1K", 2, qualities=["low"]), _row("2K", 5, qualities=["low"]),
            _row("4K", 10, qualities=["low"]),
            _row("1K", 8, qualities=["medium"]), _row("2K", 20, qualities=["medium"]),
            _row("4K", 40, qualities=["medium"]),
            _row("1K", 30, qualities=["high"]), _row("2K", 80, qualities=["high"]),
            _row("4K", 160, qualities=["high"]),
        ),
        default_cost=0, tags=(),
    ),
    UpstreamModel(
        model_id="gpt-image-2.5-sunburst", display_name="GPT Image 2.5 Sunburst",
        max_support_image_count=16, max_prompt_length=7500,
        resolutions=("1K", "2K", "4K"), default_resolutions=("2K",),
        aspect_ratios=_AR_ALL, default_aspect_ratios=("Auto",),
        costs=(
            _row("1K", 2, qualities=["low"]), _row("2K", 5, qualities=["low"]),
            _row("4K", 10, qualities=["low"]),
            _row("1K", 5, qualities=["medium"]), _row("2K", 11, qualities=["medium"]),
            _row("4K", 22, qualities=["medium"]),
            _row("1K", 8, qualities=["high"]), _row("2K", 20, qualities=["high"]),
            _row("4K", 40, qualities=["high"]),
            _row("1K", 17, qualities=["xhigh"]), _row("2K", 42, qualities=["xhigh"]),
            _row("4K", 84, qualities=["xhigh"]),
            _row("1K", 35, qualities=["max"]), _row("2K", 100, qualities=["max"]),
            _row("4K", 180, qualities=["max"]),
        ),
        default_cost=0, tags=("edit", "4k"),
    ),
    UpstreamModel(
        model_id="gpt-image-2.5-flare", display_name="GPT Image 2.5 Flare",
        max_support_image_count=16, max_prompt_length=7500,
        resolutions=("1K", "2K", "4K"), default_resolutions=("2K",),
        aspect_ratios=_AR_ALL, default_aspect_ratios=("Auto",),
        costs=(
            _row("1K", 2, qualities=["low"]), _row("2K", 5, qualities=["low"]),
            _row("4K", 10, qualities=["low"]),
            _row("1K", 5, qualities=["medium"]), _row("2K", 11, qualities=["medium"]),
            _row("4K", 22, qualities=["medium"]),
            _row("1K", 8, qualities=["high"]), _row("2K", 20, qualities=["high"]),
            _row("4K", 40, qualities=["high"]),
            _row("1K", 17, qualities=["xhigh"]), _row("2K", 42, qualities=["xhigh"]),
            _row("4K", 84, qualities=["xhigh"]),
            _row("1K", 35, qualities=["max"]), _row("2K", 100, qualities=["max"]),
            _row("4K", 180, qualities=["max"]),
        ),
        default_cost=0, tags=("edit", "4k"),
    ),
    UpstreamModel(
        model_id="gpt-image-1.5", display_name="GPT Image 1.5",
        #: 🔴 只有 3 张 —— 全表最小。按模型自己的上界校验，不要用全局经验值。
        max_support_image_count=3, max_prompt_length=2000,
        resolutions=("Low", "Medium", "High"), default_resolutions=("Low", "Medium", "High"),
        aspect_ratios=("Auto", "1:1", "3:2", "2:3"), default_aspect_ratios=("Auto",),
        costs=(_row("Low", 4), _row("Medium", 8), _row("High", 15)),
        default_cost=0, tags=("edit",),
    ),
    UpstreamModel(
        model_id="mj_v7", display_name="Midjourney V7",
        max_support_image_count=14, max_prompt_length=5000,
        resolutions=(), default_resolutions=(), aspect_ratios=_AR_ALL,
        default_aspect_ratios=("Auto",), costs=(), default_cost=3, tags=("art",),
    ),
    UpstreamModel(
        model_id="mj_niji7", display_name="Midjourney Niji7",
        max_support_image_count=14, max_prompt_length=5000,
        resolutions=(), default_resolutions=(), aspect_ratios=_AR_ALL,
        default_aspect_ratios=("Auto",), costs=(), default_cost=3, tags=("art",),
    ),
    UpstreamModel(
        model_id="image-01", display_name="Image-1.0",
        max_support_image_count=None, max_prompt_length=None,
        resolutions=(), default_resolutions=(),
        aspect_ratios=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
        default_aspect_ratios=("21:9",), costs=(), default_cost=1, tags=("cheap",),
        mode="subject-reference",
    ),
)

#: 上游**存在但未登记**的东西 —— 写在这里，是为了让"没登记"是个**决定**而不是遗漏。
DELIBERATE_ABSENCES: dict[str, str] = {
    "videoModels": "24 个视频模型由 `video/model/info` 一并返回，"
                   "但**本服务只做图片** —— 视频链路未取证，不做假能力。",
    "audioModels": "上游返回空列表（本账号无音频模型）。",
    "seedream-3.0/3.1": "若上游存在更老的 Seedream，本服务未登记：传了会被拒为未知模型。",
}

#: 上游 `referenceMode` 取值。`image-reference` = 图片参考（i2i 主路径）。
REFERENCE_MODES: frozenset[str] = frozenset({"image-reference", "subject-reference", "extend", "edit"})

#: 已知 modelKey → 上游 model_id 的**展示名**映射（快照期实测，用于日志可读性）。
_ALIAS_INDEX: dict[str, str] = {}
for _cap in CAPABILITIES:
    #: ⚠️ **能力名本身也要进索引** —— 只放 aliases 的话 `model="hailuo-i2i"`
    #: 会被判成"未知 model"，而它恰恰是最该被认出来的写法。
    _ALIAS_INDEX[_cap.name.lower()] = _cap.name
    for _a in _cap.aliases:
        _ALIAS_INDEX[_a.lower()] = _cap.name

#: 🔴 **模型别名 → 规范 upstream modelID**（2026-09-21 定义，精确匹配、区分大小写）。
#: 别名是**同一个模型的另一种拼法**：解析后与精确枚举值**同待遇 —— 零映射零说明**。
#: 只有"拼法与 modelID 不同"的才需要登记（gpt-image-* / seedream-4.5 等
#: 拼写本就一致的无需别名）。注意 `nano-banana-2`（flash，多一个连字符）与
#: `nano-banana2`（pro）只差一个 `-` —— 这是上游的命名，别"纠正"它。
MODEL_ALIASES: dict[str, str] = {
    #: `nano-banana`（无后缀）⇒ flash：调用方（newapi）的默认短名，落最快档
    "nano-banana": "nano_banana21_flash",
    "nano-banana-2": "nano_banana21_flash",
    "nano-banana-pro": "nano-banana2",
    "midjourney-v7": "mj_v7",
    "midjourney-niji7": "mj_niji7",
    "mj-v7": "mj_v7",
    "mj-niji7": "mj_niji7",
    "image-1.0": "image-01",
    "seedream-5.0-lite": "seedream-5.0",
}


def canonical_model_id(raw: str) -> str:
    """别名 → 规范 upstream modelID（非别名原样返回）。"""
    return MODEL_ALIASES.get(raw, raw)


def resolve_capability(value: str | None, *, has_image: bool) -> tuple[str, list[str]]:
    """把调用方的 `model` 解析成能力名。返回 `(能力名, 降级说明)`。

    顺序（**每一步都有依据**）：
      1. 空 / 占位名 ⇒ 按 `has_image` 推导（有图 → i2i，无图 → t2i），**留降级痕**；
      2. 能力名/别名（**大小写不敏感**）⇒ 直接命中；
      3. 上游 `model_id`（**精确、区分大小写**）⇒ **原样透传，零映射零说明**
         （能力仍按 `has_image` 推导，但那只驱动内部校验，不是"翻译"）；
      4. 都不中 ⇒ `InvalidParameterError`（由调用方抛）。
    """
    raw = (value or "").strip()
    if raw.lower() in PLACEHOLDER_MODELS:
        cap = "hailuo-i2i" if has_image else "hailuo-t2i"
        return cap, [f"未指定 model（或给了占位名 {raw!r}）⇒ 按 image "
                     f"{'非空' if has_image else '为空'} 推导为 {cap}。"]

    hit = _ALIAS_INDEX.get(raw.lower())
    if hit:
        return hit, []

    known = {m.model_id for m in all_models()}
    if raw in known or raw in MODEL_ALIASES:
        # 上游模型 key（或其别名）：**原样透传，零映射、零说明**。
        # 调用方给的就是模型本身（的另一种拼法）⇒ 不存在"翻译"，更没有降级 ——
        # 能力名只是内部校验路由（prompt/输入图校验用），不出现在上游请求里。
        # （能力按 has_image 推导：有图 → hailuo-i2i，无图 → hailuo-t2i。）
        return ("hailuo-i2i" if has_image else "hailuo-t2i"), []

    raise InvalidParameterError(
        f"未知 model={raw!r}。可用能力："
        f"{', '.join(c.name for c in CAPABILITIES)}；"
        f"或直接传上游模型 key（如 {DEFAULT_MODEL}）；"
        f"别名可用：{', '.join(sorted(MODEL_ALIASES))}。",
        param="model",
    )


def get_model(model_id: str) -> UpstreamModel | None:
    for m in FROZEN_SNAPSHOT:
        if m.model_id == model_id:
            return m
    return None


#: 运行期读到的能力表（由 `Service` 在启动/首次用时刷新）。空 = 用冻结快照。
_RUNTIME: list[UpstreamModel] = []


def install_runtime_models(models: list[UpstreamModel]) -> None:
    """装入运行期实读的模型表（`capabilities.py` 解析后调用）。"""
    global _RUNTIME
    _RUNTIME = list(models)


def runtime_models() -> list[UpstreamModel]:
    return list(_RUNTIME)


def all_models() -> list[UpstreamModel]:
    """当前生效的模型表：**优先运行期实读，退回冻结快照**。"""
    return _RUNTIME or list(FROZEN_SNAPSHOT)


def catalog() -> list[dict[str, Any]]:
    """`GET /async/v1/models` 的 `data`。**只列本服务真正支持的东西。**"""
    data = [m.to_public() for m in all_models()]
    for cap in CAPABILITIES:
        data.append({
            "id": cap.name,
            "object": "model",
            "owned_by": "hailuo-service",
            "name": cap.name,
            "kind": "capability",
            "requires_image": cap.requires_image,
            "requires_prompt": cap.requires_prompt,
            "aliases": list(cap.aliases),
            "notes": cap.notes,
        })
    return data


def capability(name: str) -> Capability | None:
    for c in CAPABILITIES:
        if c.name == name:
            return c
    return None


def status() -> dict[str, Any]:
    """`/stats` 用：能力表当前是实读的还是快照。"""
    return {
        "source": "runtime" if _RUNTIME else "frozen_snapshot",
        "snapshot_date": SNAPSHOT_DATE,
        "model_count": len(all_models()),
        "capabilities": [c.name for c in CAPABILITIES],
        "default_model": DEFAULT_MODEL,
    }


__all__ = [
    "CAPABILITIES",
    "DEFAULT_MODEL",
    "DELIBERATE_ABSENCES",
    "FROZEN_SNAPSHOT",
    "MODEL_ALIASES",
    "PLACEHOLDER_MODELS",
    "REFERENCE_MODES",
    "SNAPSHOT_DATE",
    "Capability",
    "UpstreamModel",
    "all_models",
    "canonical_model_id",
    "capability",
    "catalog",
    "get_model",
    "install_runtime_models",
    "resolve_capability",
    "runtime_models",
    "status",
]
