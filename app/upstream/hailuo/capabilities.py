#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行期读上游能力表 —— **零成本、免鉴权、不建任务**。

两个公开端点合起来才构成完整的模型注册表（缺一个都不完整）：

| 端点 | 给什么 | 缺了会怎样 |
|---|---|---|
| `GET /public/api/config/web/common_config` | `create_image_models` ⇒ **清单** + `maxSupportImageCount` + `maxPromptLength` + 展示名 + tags | 不知道**能垫几张图**（`gpt-image-1.5` 只有 3 张，拿全局经验值就会放行错误请求） |
| `GET /public/v2/api/multimodal/video/model/info` | `imageModels[]` ⇒ `parameter.resolutions/aspectRatios`（含 defaultSelect）+ `costs[]` + `defaultCost` | 不知道**合法档位与真实单价**，只能猜 |

两者都是 **GET + 无鉴权**：实测不带 `token`、不带 `yy` 也返回 200。
⇒ 读能力表**不消耗额度、不触发风控**，可以放心在启动期与缓存过期时刷新。

读不到时**不静默**：退回 `models.FROZEN_SNAPSHOT`，并把这件事写进
`Service` 的降级说明（`/stats` 的 `capabilities.source` 会显示 `frozen_snapshot`）。
"""
from __future__ import annotations

from typing import Any, Iterable

import httpx
from loguru import logger

from ...models import SNAPSHOT_DATE, FROZEN_SNAPSHOT, UpstreamModel

#: 快照索引（merge 补位用；模块级算一次）。
_SNAP_INDEX: dict[str, UpstreamModel] = {m.model_id: m for m in FROZEN_SNAPSHOT}

#: 公开端点（无鉴权、GET）。路径常量收在这里，改上游只改这两行。
COMMON_CONFIG_PATH = "/public/api/config/web/common_config"
MODEL_INFO_PATH = "/public/v2/api/multimodal/video/model/info"


def _resolutions(param: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    items = param.get("resolutions") or []
    values = tuple(str(r.get("value")) for r in items if r.get("value") is not None)
    defaults = tuple(str(r.get("value")) for r in items if r.get("defaultSelect"))
    return values, defaults


def _aspect_ratios(param: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    items = param.get("aspectRatios") or []
    values = tuple(str(r.get("value")) for r in items if r.get("value") is not None)
    defaults = tuple(str(r.get("value")) for r in items if r.get("defaultSelect"))
    return values, defaults


def parse_common_config(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从 `common_config` 抽出 `model_id -> {display_name, max_images, max_prompt, tags, mode}`。"""
    out: dict[str, dict[str, Any]] = {}
    conf = ((payload or {}).get("data") or {}).get("create_image_models") or {}
    for group in conf.get("models") or []:
        group_name = str(group.get("modelKey") or "")
        tags = tuple(str(t) for t in (group.get("filterTags") or []))
        for item in group.get("modelList") or []:
            mid = item.get("id")
            if not mid:
                continue
            out[str(mid)] = {
                "display_name": group_name or str(mid),
                "max_images": item.get("maxSupportImageCount"),
                "max_prompt": item.get("maxPromptLength"),
                "tags": tags,
                "mode": str(item.get("mode") or "image-reference"),
            }
    return out


def parse_model_info(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从 `video/model/info` 抽出 `model_id -> {resolutions, defaults, costs, default_cost}`。

    ⚠️ 端点名字里带 `video`，但响应里**同时**有 `videoModels` / `imageModels` / `audioModels`。
    只取 `imageModels` —— 本服务不做视频。
    """
    out: dict[str, dict[str, Any]] = {}
    data = (payload or {}).get("data") or {}
    for item in data.get("imageModels") or []:
        mid = item.get("modelID")
        if not mid:
            continue
        param = item.get("parameter") or {}
        resolutions, res_defaults = _resolutions(param)
        ratios, ratio_defaults = _aspect_ratios(param)
        out[str(mid)] = {
            "resolutions": resolutions,
            "default_resolutions": res_defaults,
            "aspect_ratios": ratios,
            "default_aspect_ratios": ratio_defaults,
            "costs": tuple(item.get("costs") or ()),
            "default_cost": item.get("defaultCost"),
        }
    return out


def merge(
    meta: dict[str, dict[str, Any]], info: dict[str, dict[str, Any]]
) -> list[UpstreamModel]:
    """两份数据按 `model_id` 合并。

    合并策略是**保守**的：
      · 只在 `info` 里出现的模型（有档位无清单）**保留** —— 档位更重要；
      · 只在 `meta` 里出现的模型（有清单无档位）**保留** —— 至少能校验垫图张数；
      · 键取并集，缺的字段留空/`None`，**不编造**。
    """
    merged: list[UpstreamModel] = []
    for mid in sorted(set(meta) | set(info)):
        m = meta.get(mid) or {}
        i = info.get(mid) or {}
        resolutions = tuple(i.get("resolutions") or ())
        default_res = tuple(i.get("default_resolutions") or ())
        ratios = tuple(i.get("aspect_ratios") or ())
        ratio_defaults = tuple(i.get("default_aspect_ratios") or ())
        costs = tuple(i.get("costs") or ())
        #: 🔴 快照补位（2026-10-15）：上游 `video/model/info` 的 imageModels
        #: 已不再返回 gpt-image-2.5-sunburst / -flare（实测 11 清单 vs 9 条目），
        #: 但网页前端仍对它们发 resolution/quality 并成功建任务（用户抓包）。
        #: ⇒ 档位/计价缺失时退回冻结快照（同为该端点的历史实读），
        #: 并在 `frozen_fallback` 留痕 —— 不补位会让写死默认与档位校验全部失效。
        fallback: list[str] = []
        snap = _SNAP_INDEX.get(mid)
        if snap is not None:
            if not resolutions and snap.resolutions:
                resolutions = snap.resolutions
                default_res = snap.default_resolutions
                fallback.append("resolutions")
            if not ratios and snap.aspect_ratios:
                ratios = snap.aspect_ratios
                ratio_defaults = snap.default_aspect_ratios
                fallback.append("aspect_ratios")
            if not costs and snap.costs:
                costs = snap.costs
                fallback.append("costs")
        merged.append(UpstreamModel(
            model_id=mid,
            display_name=str(m.get("display_name") or mid),
            max_support_image_count=m.get("max_images"),
            max_prompt_length=m.get("max_prompt"),
            resolutions=resolutions,
            default_resolutions=default_res,
            aspect_ratios=ratios,
            default_aspect_ratios=ratio_defaults,
            costs=costs,
            default_cost=i.get("default_cost"),
            tags=tuple(m.get("tags") or ()),
            mode=str(m.get("mode") or "image-reference"),
            frozen_fallback=tuple(fallback),
        ))
    return merged


async def fetch_models(client: httpx.AsyncClient) -> tuple[list[UpstreamModel], list[str]]:
    """拉取并合并能力表。返回 `(模型表, 降级说明)`。

    **任一子请求失败都不抛** —— 能力表读不到不该让服务起不来，
    但要**说出来**（降级说明会进 `/stats` 与响应）。
    """
    degradations: list[str] = []
    meta: dict[str, dict[str, Any]] = {}
    info: dict[str, dict[str, Any]] = {}

    for label, path, parser, sink in (
        ("create_image_models", COMMON_CONFIG_PATH, parse_common_config, meta),
        ("imageModels", MODEL_INFO_PATH, parse_model_info, info),
    ):
        try:
            resp = await client.get(path, timeout=10.0)
            resp.raise_for_status()
            sink.update(parser(resp.json()))
        except Exception as e:  # noqa: BLE001
            degradations.append(
                f"上游能力表 {label} 读取失败（{type(e).__name__}: {e}）⇒ "
                f"该部分退回冻结快照。")
            logger.warning(f"能力表 {label} 读取失败：{type(e).__name__}: {e}")

    merged = merge(meta, info)
    for m in merged:
        if m.frozen_fallback:
            degradations.append(
                f"模型 {m.model_id} 的 {'/'.join(m.frozen_fallback)} 在运行期端点缺失 "
                f"⇒ 退回冻结快照（{SNAPSHOT_DATE}）。")
    return merged, degradations


def coverage(models: Iterable[UpstreamModel]) -> dict[str, int]:
    """覆盖率自检（`/stats` 用）：有多少模型真的拿到了档位与单价。"""
    ms = list(models)
    return {
        "models": len(ms),
        "with_resolutions": sum(1 for m in ms if m.resolutions),
        "with_costs": sum(1 for m in ms if m.costs or m.default_cost),
        "with_image_limit": sum(1 for m in ms if m.max_support_image_count is not None),
    }


__all__ = [
    "COMMON_CONFIG_PATH",
    "MODEL_INFO_PATH",
    "coverage",
    "fetch_models",
    "merge",
    "parse_common_config",
    "parse_model_info",
]
