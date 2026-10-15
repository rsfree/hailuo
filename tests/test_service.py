#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""翻译层（`build_plan`）+ 提交/轮询/响应形状。

**这是本仓最该被覆盖的文件** —— `build_plan` 是纯函数，
它决定了"调用方要的东西"如何变成"上游真正做的事"，也决定了多少钱。
每一处取舍都必须能在 `degradations` 里看见。
"""
from __future__ import annotations

import pytest

from app import models
from app.errors import (
    InvalidParameterError,
    TaskNotDeletable,
    TaskNotFoundError,
)
from app.service import view
from app.store import ST_FAILURE, ST_IN_PROGRESS, ST_QUEUED, ST_SUCCEEDED
from app.upstream.hailuo.client import ST_FAIL, ST_CREATING, ST_SENSITIVE, Feed

from .conftest import PNG_1PX

IMG = "https://cdn.hailuoai.video/ref/a.png"


# ---------------------------------------------------------------------------
# 图生图（重点能力）
# ---------------------------------------------------------------------------


def test_i2i_plan_uses_image_and_prompt(service) -> None:
    """图生图：`image` 非空 + `prompt` ⇒ 能力 `hailuo-i2i`，prompt 进 `desc`。"""
    plan, cap, upstream = service.build_plan(
        {"model": "hailuo-i2i", "prompt": "a cat", "image": [IMG]})
    assert cap == "hailuo-i2i"
    assert upstream == models.DEFAULT_MODEL          # 未指定上游模型 ⇒ 落默认
    assert plan.desc == "a cat"
    assert plan.image_urls == [IMG]
    assert plan.quantity == 1                        # 默认 1（**不是上游默认张数**）
    assert plan.use_origin_prompt is True            # **不改写调用方的 prompt**
    assert plan.reference_mode is None               # 抓包里 i2i **没有**这个键 ⇒ 不发送


def test_i2i_filelist_order_is_preserved(service) -> None:
    """垫图顺序 = 请求顺序（上游按顺序理解参考语义）。"""
    urls = [f"{IMG}?i={i}" for i in range(4)]
    plan, _, _ = service.build_plan({"prompt": "x", "image": urls})
    assert plan.image_urls == urls


def test_i2i_accepts_upstream_model_key(service) -> None:
    """直接传上游 modelID（精确匹配）也能落成 i2i。

    🔴 且**零映射零说明**：名字就是上游枚举值本身，不存在"翻译"——
    `degradations` 里不许出现"落成 hailuo-t2i"这类映射痕迹。
    """
    plan, cap, upstream = service.build_plan(
        {"model": "nano_banana21_flash", "prompt": "x", "image": [IMG]})
    assert cap == "hailuo-i2i"
    assert upstream == "nano_banana21_flash"
    assert not any("落成" in d or "上游模型 key" in d
                   for d in plan.degradations)


def test_model_alias_builds_plan_with_canonical_upstream(service) -> None:
    """别名落到**规范上游模型**，同样零映射说明。

    夹具能力表只注册了 flash ⇒ 别名规范化后不在表里的模型走"落默认+留痕"
    的既有回退（这条也钉住，防止别名把回退痕迹弄丢）。
    """
    plan, cap, upstream = service.build_plan({"model": "nano-banana-2", "prompt": "x"})
    assert upstream == "nano_banana21_flash" and cap == "hailuo-t2i"
    assert not any("落成" in d for d in plan.degradations)

    plan2, _, upstream2 = service.build_plan({"model": "nano-banana-pro", "prompt": "x"})
    assert upstream2 == "nano_banana21_flash"  # 规范值 nano-banana2 不在夹具表 ⇒ 落默认
    assert any("nano-banana2" in d for d in plan2.degradations), "回退痕迹必须带规范名"


def test_i2i_rejects_subject_reference_model(service) -> None:
    """🔴 参考图模式不匹配的模型（`subject-reference`，如 image-01）带参考图
    ⇒ **本地明确拒绝**（实测 2026-09-21：打过去会被上游 `code 2400052` 拒掉，
    而调用方在上传之后才看到，毫无可执行信息）。不带图（t2i）仍可用。
    """
    from app import models as m
    from app.upstream.hailuo import capabilities as caps

    meta = {"image-01": {"display_name": "Image-1.0", "max_images": None,
                         "max_prompt": None, "tags": ("cheap",),
                         "mode": "subject-reference"}}
    info = {"image-01": {"resolutions": (), "default_resolutions": (),
                         "aspect_ratios": ("Auto",), "default_aspect_ratios": ("Auto",),
                         "costs": (), "default_cost": 1}}
    m.install_runtime_models(caps.merge(meta, info))

    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"model": "image-01", "prompt": "x", "image": [IMG]})
    assert "主体参考" in str(ei.value) and "image-reference" in str(ei.value)

    _, cap, upstream = service.build_plan({"model": "image-01", "prompt": "x"})
    assert upstream == "image-01" and cap == "hailuo-t2i", "不带图仍可走文生图"


def test_i2i_requires_image(service) -> None:
    """显式要 i2i 却不给图 ⇒ 400，并告诉他该怎么做。"""
    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"model": "hailuo-i2i", "prompt": "x", "image": []})
    assert ei.value.param == "image"
    assert "图生图" in str(ei.value)


def test_i2i_requires_prompt(service) -> None:
    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"model": "hailuo-i2i", "image": [IMG]})
    assert ei.value.param == "prompt"


def test_image_string_is_rejected_with_fix_instructions(service) -> None:
    """`"image": "https://…"` 是常见误写 ⇒ **明确拒绝并给出正确写法**。

    🔴 绝不把它当单元素数组 —— 那会让"我传了几张"变得不可预测。
    """
    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"prompt": "x", "image": IMG})
    assert ei.value.param == "image"
    assert '["' in str(ei.value)          # 提示里给了正确写法


# ---------------------------------------------------------------------------
# 张数上限 —— **按所选模型自己声明的**，不是全局经验值
# ---------------------------------------------------------------------------


def test_image_limit_uses_model_declared_cap() -> None:
    """`gpt-image-2.5-flare` 声明 16 张 ⇒ 传 17 张必须 400（**不是静默丢弃**）。

    ⚠️ 2026-10-15 起旧枚举 gpt-image-1.5（上限 3）已被成本策略**定向**到
    flare —— 上限校验仍按"定向后的模型"执行（flare=16），旧拼法传 17 张
    同样超限 400，意图不变。
    """
    models.install_runtime_models(list(models.FROZEN_SNAPSHOT))
    from app.config import Settings
    from app.service import Service

    st = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t")
    st.validate()
    svc = Service(st, fetch_capabilities=False)
    try:
        m = models.get_model("gpt-image-2.5-flare")
        assert m is not None and m.max_support_image_count == 16
        with pytest.raises(InvalidParameterError) as ei:
            svc.build_plan({"model": "gpt-image-2.5-flare", "prompt": "x",
                            "image": [f"{IMG}?i={i}" for i in range(17)]})
        assert ei.value.param == "image"
        assert "16" in str(ei.value)
        # 16 张（= 上限）必须放行
        plan, _, _ = svc.build_plan({"model": "gpt-image-2.5-flare", "prompt": "x",
                                     "image": [f"{IMG}?i={i}" for i in range(16)]})
        assert len(plan.image_urls) == 16
    finally:
        svc.close()


def test_off_table_ratio_snaps_to_nearest() -> None:
    """🔴 表外比例自动映射（2026-10-15 用户口径）：flare 不支持 1:4/8:1 等
    极端比例 ⇒ 吸附到最近支持档并留痕，不 400。"""
    from app import models as m2

    m2.install_runtime_models(list(m2.FROZEN_SNAPSHOT))
    from app.config import Settings
    from app.service import Service

    st = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t")
    st.validate()
    svc = Service(st, fetch_capabilities=False)
    try:
        cases = {"1:4": "9:16", "4:1": "21:9", "1:8": "9:16", "8:1": "21:9"}
        for raw, expect in cases.items():
            plan, _, _ = svc.build_plan(
                {"model": "gpt-image-2.5-flare", "prompt": "x",
                 "aspect_ratio": raw, "resolution": "1K"})
            assert plan.aspect_ratio == expect, f"{raw} ⇒ {plan.aspect_ratio}"
            joined = " ".join(plan.degradations)
            assert "自动映射" in joined and raw in joined, raw
        # i2i 的 Auto 保留、t2i 的 Auto 丢弃（防回归）
        plan_a, _, _ = svc.build_plan(
            {"model": "gpt-image-2.5-flare", "prompt": "x", "aspect_ratio": "Auto"})
        assert plan_a.aspect_ratio is None
    finally:
        svc.close()
        m2.install_runtime_models([])


def test_gpt_image_family_redirects_to_flare() -> None:
    """🔴 成本策略定向（2026-10-15）：gpt-image* 一律落 gpt-image-2.5-flare。"""
    models.install_runtime_models(list(models.FROZEN_SNAPSHOT))
    from app.config import Settings
    from app.service import Service

    st = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t")
    st.validate()
    svc = Service(st, fetch_capabilities=False)
    try:
        for alias in ("gpt-image-2", "gpt-image-2.5-sunburst",
                      "gpt-image-1.5", "gpt-image", "gpt-image-1"):
            plan, _, upstream = svc.build_plan(
                {"model": alias, "prompt": "x"})
            assert upstream == "gpt-image-2.5-flare", alias
            assert plan.upstream_model == "gpt-image-2.5-flare"
            assert any("定向" in d for d in plan.degradations), alias
        # 1.5 档位词汇随之翻译（High ⇒ 4K ⇒ 超分 ×4）
        plan, _, _ = svc.build_plan(
            {"model": "gpt-image-1.5", "prompt": "x", "resolution": "High"})
        assert plan.requested_resolution == "4K" and plan.superres_magnify == 4
        assert plan.resolution == "1K" and plan.quality == "low"
        # flare 精确拼法不受影响
        _, _, exact = svc.build_plan(
            {"model": "gpt-image-2.5-flare", "prompt": "x"})
        assert exact == "gpt-image-2.5-flare"
    finally:
        svc.close()


def test_nano_banana_allows_more_than_four() -> None:
    """`nano_banana21_flash` 声明 14 张 ⇒ 5 张要放行（旧实现写死 4 张会误拒）。"""
    models.install_runtime_models(list(models.FROZEN_SNAPSHOT))
    from app.config import Settings
    from app.service import Service

    st = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t")
    st.validate()
    svc = Service(st, fetch_capabilities=False)
    try:
        plan, _, _ = svc.build_plan({"prompt": "x",
                                     "image": [f"{IMG}?i={i}" for i in range(5)]})
        assert len(plan.image_urls) == 5
        # 超过 4 张必须**留痕**（多图语义未逐一取证）
        assert any("垫图" in d for d in plan.degradations)
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# 文生图
# ---------------------------------------------------------------------------


def test_t2i_without_image(service) -> None:
    plan, cap, _ = service.build_plan({"model": "hailuo-t2i", "prompt": "一只猫"})
    assert cap == "hailuo-t2i"
    assert plan.image_urls == []
    assert plan.desc == "一只猫"


def test_auto_derivation_from_image_presence(service) -> None:
    """不写 model ⇒ 按 image 是否为空推导，**并留痕**。"""
    p1, c1, _ = service.build_plan({"prompt": "x"})
    p2, c2, _ = service.build_plan({"prompt": "x", "image": [IMG]})
    assert (c1, c2) == ("hailuo-t2i", "hailuo-i2i")
    assert any("推导" in d for d in p1.degradations)
    assert any("推导" in d for d in p2.degradations)


def test_placeholder_model_equals_no_model(service) -> None:
    """第三方 SDK 硬编码的 `dall-e-3` 等占位名 ⇒ 等价于没写 model。"""
    plan, cap, _ = service.build_plan({"model": "dall-e-3", "prompt": "x"})
    assert cap == "hailuo-t2i"
    assert any("占位" in d or "未指定" in d for d in plan.degradations)


def test_unknown_model_is_rejected(service) -> None:
    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"model": "sora-9000", "prompt": "x"})
    assert ei.value.param == "model"
    assert "未知 model" in str(ei.value)


# ---------------------------------------------------------------------------
# 未知字段：区分"上游没有"与"你写错了"
# ---------------------------------------------------------------------------


def test_upstream_absent_fields_go_to_degradations(service) -> None:
    """`watermark` / `stream` 这类"上游没有"的字段 ⇒ 不报错，进 degradations。"""
    plan, _, _ = service.build_plan({"prompt": "x", "watermark": True, "stream": False})
    joined = " ".join(plan.degradations)
    assert "watermark" in joined and "stream" in joined
    assert "已忽略" in joined


def test_typo_field_is_400(service) -> None:
    """写错的字段名 ⇒ 400（他的修复动作是改请求体，不是查上游能力表）。"""
    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"prompt": "x", "promt": "typo"})
    assert ei.value.param == "promt"


# ---------------------------------------------------------------------------
# n（张数）
# ---------------------------------------------------------------------------


def test_n_defaults_to_one(service) -> None:
    """不传 n ⇒ **1**。绝不采用上游的默认张数（那会让调用方按 1 张的预期收到多张账单）。"""
    plan, _, _ = service.build_plan({"prompt": "x"})
    assert plan.quantity == 1
    assert not any("吸附" in d for d in plan.degradations)


def test_n_over_cap_is_clamped_and_noted(service) -> None:
    plan, _, _ = service.build_plan({"prompt": "x", "n": 99})
    assert plan.quantity == 10
    assert any("上限 10" in d for d in plan.degradations)


@pytest.mark.parametrize("bad", [0, -1, "3"])
def test_bad_n_is_400(service, bad) -> None:
    with pytest.raises(InvalidParameterError):
        service.build_plan({"prompt": "x", "n": bad})


# ---------------------------------------------------------------------------
# size / resolution / aspect_ratio
# ---------------------------------------------------------------------------


def test_size_maps_to_resolution_and_ratio_with_degradation(service) -> None:
    """`size` 不是上游概念 ⇒ 换算 + 吸附，**两件都必须留痕**。"""
    plan, _, _ = service.build_plan({"prompt": "x", "size": "4096x4096"})
    assert plan.resolution == "4K"
    joined = " ".join(plan.degradations)
    assert "size=4096x4096" in joined and "resolution=4K" in joined
    assert "aspect_ratio" in joined


def test_size_snaps_to_nearest_supported_ratio(service) -> None:
    """1024x1024 的 1:1 在支持列表里 ⇒ 取 1:1（而不是最近的别的）。"""
    plan, _, _ = service.build_plan({"prompt": "x", "size": "1024x1024"})
    assert plan.aspect_ratio == "1:1"
    assert plan.resolution == "1K"


def test_exact_ratio_is_reported_as_exact_not_snapped(service) -> None:
    """🔴 **精确对应 ≠ 吸附** —— 措辞必须分开。

    说成"吸附"会让人以为自己给的比例被改了，于是去改一个本来正确的参数。
    只有真的被改过（取近似档）才用"吸附"。
    """
    exact, _, _ = service.build_plan({"prompt": "x", "size": "1024x1024"})
    joined_exact = " ".join(exact.degradations)
    assert "正好对应 aspect_ratio=1:1" in joined_exact
    assert "吸附" not in joined_exact

    # 15:10 = 3:2 不在候选里 ⇒ 真的需要吸附，这时才说"吸附"
    snapped, _, _ = service.build_plan({"prompt": "x", "size": "1500x1000"})
    joined_snap = " ".join(snapped.degradations)
    assert "**吸附**" in joined_snap
    assert snapped.aspect_ratio is not None


def test_bad_size_format_is_400(service) -> None:
    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"prompt": "x", "size": "big"})
    assert ei.value.param == "size"


def test_native_resolution_validated_against_model(service) -> None:
    """原生 `resolution` 按**该模型声明的枚举**校验。"""
    plan, _, _ = service.build_plan({"prompt": "x", "resolution": "2K"})
    assert plan.resolution == "2K"
    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"prompt": "x", "resolution": "8K"})
    assert ei.value.param == "resolution"


def test_defaults_pin_cheapest_tier(service) -> None:
    """不指定档位 ⇒ **写死最便宜档**（2026-10-15 用户口径），不再跟随 defaultSelect。

    假上游 1K/2K/4K 全部 defaultSelect ⇒ 取排序最小仍为 1K，
    但降级说明必须说清是"写死"而不是"上游默认"。
    """
    plan, _, _ = service.build_plan({"prompt": "x"})
    assert plan.resolution == "1K"
    assert plan.aspect_ratio is None  # Auto 是 i2i 语义 ⇒ t2i 不发送该键
    joined = " ".join(plan.degradations)
    assert "写死最便宜档" in joined
    assert "defaultSelect" in joined          # 说明里仍交代上游默认是什么、为何不用
    assert "已不作为默认" in joined
    # nano 无 quality 维度 ⇒ 写死不适用于它，quality 不应出现
    assert plan.quality is None


def test_flare_defaults_pin_1k_low() -> None:
    """🔴 `gpt-image-2.5-flare` 默认 = 1K + low（用户口径 2026-10-15）。

    flare 的 defaultSelect 是 **2K**，若跟随上游默认，"没指定"的调用方
    要为 1K 能做的事付 2.5 倍积分（2K low=5 vs 1K low=2）。
    且 gpt-image-* 按 resolution×quality 计价，quality 不带键时上游按哪档
    收费未取证 ⇒ 写死 low，把"未知"变成"已知"。
    """
    from app import models as m

    m.install_runtime_models(list(m.FROZEN_SNAPSHOT))
    from app.config import Settings
    from app.service import Service

    st = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t")
    st.validate()
    svc = Service(st, fetch_capabilities=False)
    try:
        plan, _, upstream = svc.build_plan(
            {"model": "gpt-image-2.5-flare", "prompt": "a dog", "aspect_ratio": "21:9"})
        assert upstream == "gpt-image-2.5-flare"
        assert plan.resolution == "1K", "defaultSelect=2K 必须被写死口径覆盖为 1K"
        assert plan.quality == "low"
        assert plan.aspect_ratio == "21:9"   # 显式传值仍透传
        assert plan.forecast_credits == 2.0  # 1K×low 真实单价
        joined = " ".join(plan.degradations)
        assert "写死最便宜档 1K" in joined
        assert "写死 low" in joined

        # 显式传更高档 ⇒ 🔴 成本策略覆盖：上游仍 1K/low，请求档位转超分计划
        plan2, _, _ = svc.build_plan(
            {"model": "gpt-image-2.5-flare", "prompt": "x",
             "resolution": "4K", "quality": "high"})
        assert plan2.resolution == "1K" and plan2.quality == "low"
        assert plan2.requested_resolution == "4K"
        assert plan2.superres_magnify == 4
        assert plan2.forecast_credits == 2.0  # 计费口径也是 1K/low
        joined2 = " ".join(plan2.degradations)
        assert "gpt-image 成本策略" in joined2 and "×4" in joined2
    finally:
        svc.close()
        m.install_runtime_models([])  # 还原：别污染其他用例的运行期表


# ---------------------------------------------------------------------------
# prompt 长度上限
# ---------------------------------------------------------------------------


def test_prompt_over_model_limit_is_400(service) -> None:
    """上限来自上游 `maxPromptLength`（7500），不是拍脑袋。"""
    with pytest.raises(InvalidParameterError) as ei:
        service.build_plan({"prompt": "x" * 7501})
    assert ei.value.param == "prompt"
    assert "7500" in str(ei.value)


def test_prompt_within_limit_ok(service) -> None:
    plan, _, _ = service.build_plan({"prompt": "x" * 7500})
    assert len(plan.desc) == 7500


# ---------------------------------------------------------------------------
# promptStruct
# ---------------------------------------------------------------------------


def test_prompt_struct_matches_captured_shape() -> None:
    """`promptStruct` 复刻抓包形态（单段落 + 三个长度字段）。

    抓包回显（t2i「a cat」）：
    `{"value":[{"type":"paragraph","children":[{"text":"a cat"}]}],"length":5,"plainLength":5,"rawLength":5}`
    """
    import json

    from app.service import Plan

    plan = Plan(capability="hailuo-t2i", upstream_model="m", desc="a cat")
    got = json.loads(plan.prompt_struct())
    assert got == {"value": [{"type": "paragraph", "children": [{"text": "a cat"}]}],
                   "length": 5, "plainLength": 5, "rawLength": 5}


# ---------------------------------------------------------------------------
# 受理：落库 + 零上游往返
# ---------------------------------------------------------------------------


def test_create_stores_and_returns_task_id(service, fake) -> None:
    rec = service.create({"model": "hailuo-i2i", "prompt": "a cat", "image": [IMG]},
                         credential="cred-abc")
    assert rec["task_id"].startswith("hailuo_")
    assert rec["status"] == ST_QUEUED
    # 🔴 受理**零上游往返**
    assert fake.create_calls == []
    assert fake.batch_calls == 0
    assert fake.policy_calls == 0
    # 计划已落库（协调器随后据此建任务）
    full = service.store.get_full(rec["task_id"])
    assert full["plan"]["image_urls"] == [IMG]
    assert full["capability"] == "hailuo-i2i"


def test_create_records_degradations(service) -> None:
    rec = service.create({"prompt": "x"}, credential="c")
    assert rec["degradations"], "推导/默认档位应当留下降级说明"
    assert any("推导" in d for d in rec["degradations"])


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


def test_submit_dry_run_sends_nothing(service, fake) -> None:
    """`dry_run=True` —— 签名与 body 全算出来，但**零消耗**。

    ⚠️ 边界要说清：dry_run 仍会**下载**输入图（只读、免费，且要据此算 md5 与格式），
    但**不会**上传、**不会**建任务 —— 后两者才是会改变上游状态的动作。
    """
    fake.add_image(IMG, PNG_1PX)
    rec = service.create({"model": "hailuo-i2i", "prompt": "a cat", "image": [IMG]},
                         credential="c")
    res = service.submit(rec["task_id"], dry_run=True)
    assert res["dry_run"] is True
    assert fake.create_calls == [], "dry_run 不许建任务"
    assert fake.callback_calls == [], "dry_run 不许真上传"
    assert fake.policy_calls == 0, "dry_run 连上传凭据都不该取"
    # 但翻译层确实算过：fileList 已经构造出来了
    entry = res["file_list"][0]
    assert entry["id"] == "<dry-run-file-id>"
    assert entry["frameType"] == 3 and entry["assetFileType"] == 1


def test_submit_builds_correct_upstream_body(service, fake) -> None:
    """真提交（打的是**假上游**）：请求体逐字段检查。"""
    fake.add_image(IMG, PNG_1PX)
    rec = service.create(
        {"model": "nano_banana21_flash", "prompt": "a cat", "image": [IMG],
         "n": 2, "size": "4096x4096"}, credential="c")
    res = service.submit(rec["task_id"])
    assert res["submitted"] is True
    assert res["upstream_batch_id"]

    body = fake.create_calls[0]
    assert body["projectID"] == "0"
    assert body["quantity"] == 2
    p = body["parameter"]
    assert p["modelID"] == "nano_banana21_flash"
    assert p["desc"] == "a cat"
    assert p["useOriginPrompt"] is True
    # 🔴 抓包的 i2i 里**没有** referenceMode ⇒ 默认不发送
    assert "referenceMode" not in p
    # fileList 一项就是抓包形态的字段集
    entry = p["fileList"][0]
    for key in ("id", "name", "type", "url", "frameType", "referenceType",
                "assetFileType", "duration", "characterID", "characterUrl",
                "videoID", "durationMs"):
        assert key in entry, f"fileList 缺 {key}（上游需要它才认参考图）"
    assert entry["frameType"] == 3
    assert entry["assetFileType"] == 1
    assert entry["duration"] == 0


def test_submit_uploads_each_image_once_then_caches(service, fake) -> None:
    """同内容图只上传一次（上传结果按 md5 缓存）。"""
    fake.add_image(IMG, PNG_1PX)
    rec1 = service.create({"prompt": "a", "image": [IMG]}, credential="c")
    service.submit(rec1["task_id"])
    assert fake.policy_calls == 1

    rec2 = service.create({"prompt": "b", "image": [IMG]}, credential="c")
    service.submit(rec2["task_id"])
    assert fake.policy_calls == 1, "第二次应命中上传缓存"


def test_submit_ingests_images_in_parallel(service, fake) -> None:
    """多张输入图**并行 ingest**（下载→归一化→上传 每张独立流水线）。

    并发计数是决定性证据（墙钟断言在 CI 会抖）；同时钉住
    🔴 fileList 顺序 = 请求顺序（`pool.map` 保序，上游按顺序理解垫图语义）。
    ⚠️ 三张图必须**字节互异**：相同内容会命中 md5 上传缓存去重
    （那是正确行为——同内容只传一次），会让顺序断言失去意义。
    """
    from .conftest import _make_png

    urls = [f"https://cdn.hailuoai.video/ing{i}.png" for i in range(3)]
    for i, u in enumerate(urls):
        fake.add_image(u, _make_png(8 + i, 8 + i))  # 尺寸不同 ⇒ 字节互异 ⇒ md5 互异
    rec = service.create({"prompt": "x", "image": urls}, credential="c")
    service.submit(rec["task_id"])

    assert fake.download_max_concurrency >= 2, "并行 ingest 必须真的并发下载"
    assert fake.policy_calls == 1, "凭据是账号级的：三张图只取**一次**（短 TTL 缓存+单飞）"
    entries = fake.create_calls[0]["parameter"]["fileList"]
    assert [e["name"] for e in entries] == ["ing0.png", "ing1.png", "ing2.png"]
    assert len({e["id"] for e in entries}) == 3, "三张图必须各得一个 fileID"


def test_submit_accepts_inline_base64_image(service, fake) -> None:
    """**本地图片**（base64 data URL）也能走完全链路 —— 无需先传到公网。"""
    import base64

    payload = base64.b64encode(PNG_1PX).decode()
    rec = service.create(
        {"prompt": "x", "image": [f"data:image/png;base64,{payload}"]}, credential="c")
    service.submit(rec["task_id"])

    entry = fake.create_calls[0]["parameter"]["fileList"][0]
    assert entry["id"], "内联图也要走完上传拿到 fileID（不能跳过上传）"
    assert entry["name"].startswith("inline-")
    assert entry["assetFileType"] == 1


def test_pre_create_failure_is_marked_for_retry(service, fake) -> None:
    """🔴 建任务**之前**的 5xx 类失败要打 `pre_create` 标记（协调器据此重试）；
    4xx（调用方问题）**不标记** —— 重试无意义。"""
    import httpx

    from app.errors import InvalidParameterError, UpstreamError

    fake.add_image(IMG, PNG_1PX)
    fake.force_error["/v1/api/files/request_policy"] = httpx.Response(500, text="boom")
    rec = service.create({"prompt": "x", "image": [IMG]}, credential="c")
    with pytest.raises(UpstreamError) as ei:
        service.submit(rec["task_id"])
    assert getattr(ei.value, "pre_create", False) is True
    assert fake.create_calls == [], "失败发生在建任务前，绝不能建任务"

    # 4xx：内容策略类（不可重试）—— 不得打 pre_create 标记
    fake.force_error["/v1/api/files/request_policy"] = httpx.Response(
        200, json={"statusInfo": {"code": 32, "message": "内容包含敏感信息"}})
    rec2 = service.create({"prompt": "x", "image": [IMG]}, credential="c")
    with pytest.raises((InvalidParameterError, Exception)) as ei2:
        service.submit(rec2["task_id"])
    assert getattr(ei2.value, "pre_create", False) is False


def test_submit_without_credential_is_503(settings, store, fake) -> None:
    """**透传语义**：任务的凭据不在池里（进程重启/过期/从未登记）⇒ 503。

    这是**服务端状态问题**，不是调用方凭据错（那次请求当时合法）⇒
    503 `credential_unavailable`，而**不是** 401。
    🔴 且绝不允许"回落到别的账号" —— 那会把费用记到错误的人头上。
    """

    from app.errors import CredentialUnavailable
    from app.service import Service

    #: 不注入 client/uploader ⇒ 走透传池；凭据从未登记 ⇒ 必然命中"池里没有"
    svc = Service(settings, store=store, fetch_capabilities=False,
                  http_transport=fake.transport())
    try:
        rec = svc.create({"prompt": "x"}, credential="never-registered")
        with pytest.raises(CredentialUnavailable) as ei:
            svc.submit(rec["task_id"])
        assert ei.value.status_code == 503
        assert "重新提交" in str(ei.value)
    finally:
        svc.close()


def test_poll_fails_tasks_whose_credential_vanished(settings, store, fake) -> None:
    """在途任务的凭据消失（重启）⇒ 该组任务**明确失败**，不留僵尸。

    语义取舍：任务全生命周期都需要这个 token（轮询也要）⇒ 拿不出就永远推进不了，
    明说原因比静默卡死诚实。
    """
    from app.errors import CredentialUnavailable
    from app.service import Service

    svc = Service(settings, store=store, fetch_capabilities=False,
                  http_transport=fake.transport())
    try:
        rec = svc.create({"prompt": "x"}, credential="ghost")
        svc.store.update_task(rec["task_id"], status="in_progress",
                              upstream_batch_id="b-ghost", submitted_at=1.0)
        svc.poll_many(svc.store.in_flight())
        full = svc.store.get_full(rec["task_id"])
        assert full["status"] == "failure"
        assert full["error"]["code"] == "credential_unavailable"
        assert "内存池" in full["error"]["message"]
        _ = CredentialUnavailable  # 语义锚点：失败原因就是这个类
    finally:
        svc.close()


def test_clients_are_routed_per_credential(settings, store, fake) -> None:
    """🔴 **按凭据路由**：两个 JWT 各自的建任务请求必须带**各自的 token** 出站。"""
    from .conftest import FAKE_JWT, FAKE_JWT_B
    from app.service import Service

    svc = Service(settings, store=store, fetch_capabilities=False,
                  http_transport=fake.transport())
    try:
        fp_a = svc.register_credential(FAKE_JWT)
        fp_b = svc.register_credential(FAKE_JWT_B)
        assert fp_a != fp_b, "不同 token ⇒ 不同指纹（任务隔离的基础）"

        for fp in (fp_a, fp_b):
            rec = svc.create({"prompt": "x"}, credential=fp)
            svc.submit(rec["task_id"])

        assert len(fake.create_calls) == 2
        #: 两条建任务分别带着 A / B 的 token（顺序=提交顺序）
        assert fake.tokens_seen == [FAKE_JWT, FAKE_JWT_B], fake.tokens_seen[:3]
        #: 两个凭据 ⇒ 两套客户端（各自独立连接池与上传缓存）
        assert svc.credentials.stats()["entries"] == 2
    finally:
        svc.close()


# ---------------------------------------------------------------------------
# 轮询
# ---------------------------------------------------------------------------


def test_poll_uses_one_upstream_call_for_many_tasks(service, fake) -> None:
    """🔴 **一轮 tick 只打一次上游** —— 与在途任务数无关。"""
    fake.add_image(IMG, PNG_1PX)
    for i in range(4):
        rec = service.create({"prompt": f"p{i}", "image": [IMG]}, credential="c")
        service.submit(rec["task_id"])
    assert len(fake.create_calls) == 4

    before = fake.batch_calls
    tasks = service.store.in_flight()
    assert len(tasks) == 4
    service.poll_many(tasks)
    assert fake.batch_calls - before == 1, "4 个在途任务必须只产生 1 次上游查询"


def test_poll_marks_success_and_exposes_url(service, fake) -> None:
    fake.add_image(IMG, PNG_1PX)
    rec = service.create({"prompt": "a cat", "image": [IMG]}, credential="c")
    service.submit(rec["task_id"])
    service.poll_many(service.store.in_flight())

    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_SUCCEEDED
    code, body = view(full)
    assert code == 200
    assert body["data"], "产物 URL 必须出现在 data[]"
    assert body["data"][0]["url"].startswith("https://")
    assert body["usage"]["images"] == 1
    assert body["created"] > 0


def test_poll_keeps_non_terminal_for_creating(service, fake) -> None:
    fake.add_image(IMG, PNG_1PX)
    rec = service.create({"prompt": "a cat", "image": [IMG]}, credential="c")
    service.submit(rec["task_id"])
    batch_id = next(iter(fake.feed_status))
    fake.set_feed(batch_id, status=ST_CREATING)

    service.poll_many(service.store.in_flight())
    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_IN_PROGRESS
    code, body = view(full)
    assert code == 202 and body["status"] == ST_IN_PROGRESS


def test_poll_marks_failure_with_code(service, fake) -> None:
    fake.add_image(IMG, PNG_1PX)
    rec = service.create({"prompt": "a cat", "image": [IMG]}, credential="c")
    service.submit(rec["task_id"])
    batch_id = next(iter(fake.feed_status))
    fake.set_feed(batch_id, status=ST_SENSITIVE)

    service.poll_many(service.store.in_flight())
    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_FAILURE
    assert full["error"]["code"] == "content_policy_violation"
    code, body = view(full)
    # 🔴 2026-10-15 用户口径：内容违规 ⇒ 451（唯一例外，其余失败仍 200）
    assert code == 451
    assert body["status"] == "failure"
    # 敏感终态同时写负缓存 ⇒ 同 prompt/图组合的二次受理直接 451
    key = service._negcache_key("a cat", [IMG])
    assert service._negcache_hit(key)


def test_poll_ignores_unknown_batches(service, fake) -> None:
    """上游窗口里出现了别的任务 ⇒ 不能误改本服务任何任务的状态。"""
    fake.set_feed("999999999999999999", status=ST_FAIL)
    rec = service.create({"prompt": "x"}, credential="c")
    service.poll_many([{"task_id": rec["task_id"], "upstream_batch_id": "111"}])
    assert service.store.get_full(rec["task_id"])["status"] == ST_QUEUED


def test_poll_falls_back_to_v4_when_task_misses_window(service, fake) -> None:
    """任务被挤出 `my/batch` 窗口 ⇒ 合并成**一次** v4 点名直查，照样推进终态。

    这条补的是"我最近 N 条"的盲区：账号历史一多，在途任务可能不在窗口里，
    以前只能干等（保持非终态直到超时看门狗收掉）。
    """
    fake.add_image(IMG, PNG_1PX)
    rec = service.create({"prompt": "a cat", "image": [IMG]}, credential="c")
    service.submit(rec["task_id"])
    batch_id = next(iter(fake.feed_status))
    fake.hide_from_batch(batch_id)  # 主窗口看不见它
    fake.set_v4(batch_id, status=2, url="https://cdn.hailuoai.video/fake/v4.png",
                percent=100)

    res = service.poll_many(service.store.in_flight())
    assert len(fake.v4_calls) == 1, "兜底必须合并成一次 v4 查询"
    assert fake.v4_calls[0]["batchInfoList"] == [{"batchID": batch_id, "batchType": 1}]
    assert res["fallback"]["updated"] == 1

    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_SUCCEEDED
    _, body = view(full)
    assert body["data"][0]["url"] == "https://cdn.hailuoai.video/fake/v4.png"


def test_poll_fallback_failure_keeps_non_terminal(service, fake) -> None:
    """v4 兜底挂了 ⇒ 不推进任何状态（"查不到"不是"任务失败"）。"""
    import httpx

    fake.add_image(IMG, PNG_1PX)
    rec = service.create({"prompt": "a cat", "image": [IMG]}, credential="c")
    service.submit(rec["task_id"])
    batch_id = next(iter(fake.feed_status))
    fake.hide_from_batch(batch_id)
    fake.force_error["/v4/api/multimodal/video/processing"] = httpx.Response(
        200, json={"statusInfo": {"code": 99, "message": "内部错误"}})

    res = service.poll_many(service.store.in_flight())
    assert "error" in res["fallback"]
    assert service.store.get_full(rec["task_id"])["status"] == ST_IN_PROGRESS


def test_poll_skips_v4_when_window_covers_all_tasks(service, fake) -> None:
    """全部任务都在主窗口里 ⇒ 一轮仍然**恒 1 次**上游查询（v4 不打）。"""
    fake.add_image(IMG, PNG_1PX)
    rec = service.create({"prompt": "a cat", "image": [IMG]}, credential="c")
    service.submit(rec["task_id"])

    service.poll_many(service.store.in_flight())
    assert fake.v4_calls == [], "窗口覆盖全部任务时兜底必须静默"


# ---------------------------------------------------------------------------
# 多张任务（quantity>1）的收口语义 —— 2026-09-21 n=2 实测教训
# ---------------------------------------------------------------------------


def _feed(fid: str, status: int, url: str = "") -> Feed:
    return Feed(feed_id=fid, batch_id="b", status=status, feed_type=1,
                create_time=1, url=url)


def test_apply_feeds_waits_until_quantity_ready(service) -> None:
    """n=2 只到 1 张成功、另一条还在途 ⇒ **不许提前终态**（否则第二张白花钱）。"""
    rec = service.create({"prompt": "x", "n": 2}, credential="c")
    feeds = [_feed("f1", 2, "https://cdn/1.png"), _feed("f2", 1)]
    assert service._apply_feeds(rec["task_id"], feeds) is False
    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_QUEUED  # 未推进任何终态
    assert full["upstream_status"] == 2  # 只刷新在途状态（feeds 状态取 max）


def test_apply_feeds_aggregates_all_when_quantity_ready(service) -> None:
    """n=2 两条都成功 ⇒ 终态成功，**聚合 2 个产物**。"""
    rec = service.create({"prompt": "x", "n": 2}, credential="c")
    feeds = [_feed("f1", 2, "https://cdn/1.png"), _feed("f2", 2, "https://cdn/2.png")]
    assert service._apply_feeds(rec["task_id"], feeds) is True
    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_SUCCEEDED
    assert len(full["result"]["data"]) == 2


def test_apply_feeds_partial_failure_still_delivers(service) -> None:
    """n=2 一条成功一条失败 ⇒ 按已有产物**成功交付**（部分成功也是交付）。"""
    rec = service.create({"prompt": "x", "n": 2}, credential="c")
    feeds = [_feed("f1", 2, "https://cdn/1.png"), _feed("f2", 3)]
    assert service._apply_feeds(rec["task_id"], feeds) is True
    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_SUCCEEDED
    assert len(full["result"]["data"]) == 1


def test_apply_feeds_all_failed_is_failure(service) -> None:
    """n=2 全部失败 ⇒ 终态失败。"""
    rec = service.create({"prompt": "x", "n": 2}, credential="c")
    feeds = [_feed("f1", 3), _feed("f2", 5)]
    assert service._apply_feeds(rec["task_id"], feeds) is True
    full = service.store.get_full(rec["task_id"])
    assert full["status"] == ST_FAILURE
    assert full["error"]["code"] in ("task_failed", "content_policy_violation")


# ---------------------------------------------------------------------------
# 响应形状（唯一出口）
# ---------------------------------------------------------------------------


def test_view_never_exposes_degradations() -> None:
    """降级说明**不进 HTTP 响应**（2026-10-10 接口字段收敛）：只进日志与任务库。"""
    code, body = view({"task_id": "t", "status": ST_QUEUED, "degradations": []})
    assert code == 202 and "degradations" not in body
    code, body = view({"task_id": "t", "status": ST_QUEUED,
                       "degradations": ["x"]})
    assert code == 202 and "degradations" not in body


def test_view_success_has_no_degradations_key_when_empty() -> None:
    code, body = view({
        "task_id": "t", "status": ST_SUCCEEDED, "degradations": [],
        "result": {"data": [{"url": "u"}], "created": 1, "images": 1},
        "plan": {"forecast_credits": 4}})
    assert code == 200
    assert body["data"] == [{"url": "u"}]
    assert body["usage"] == {"images": 1, "forecast_credits": 4}
    assert "degradations" not in body


def test_view_only_exposes_url_in_data() -> None:
    """`data[]` 里**只有 url** —— 与冻结契约逐字一致（多一个键就多一分形状风险）。"""
    code, body = view({
        "task_id": "t", "status": ST_SUCCEEDED, "degradations": [],
        "result": {"data": [{"url": "u"}], "created": 1, "images": 1,
                   "width": 4096, "height": 4096}, "plan": {}})
    assert set(body["data"][0]) == {"url"}


def test_view_forecast_credits_absent_when_unknown() -> None:
    """查不到单价时**不给** `forecast_credits` —— 编一个数字等于说谎。"""
    code, body = view({"task_id": "t", "status": ST_SUCCEEDED, "degradations": [],
                       "result": {"data": [{"url": "u"}], "created": 1, "images": 1},
                       "plan": {}})
    assert "forecast_credits" not in body["usage"]


# ---------------------------------------------------------------------------
# 归属与删除
# ---------------------------------------------------------------------------


def test_get_for_credential_scopes_by_key(service) -> None:
    rec = service.create({"prompt": "x"}, credential="cred-A")
    assert service.get_for_credential(rec["task_id"], "cred-A")["task_id"] == \
        rec["task_id"]
    with pytest.raises(TaskNotFoundError):
        service.get_for_credential(rec["task_id"], "cred-B")


def test_get_without_key_is_allowed(service) -> None:
    """**id 即凭据**：不带 Key 也能查（但带了错的 Key 仍 401，见 test_api）。"""
    rec = service.create({"prompt": "x"}, credential="cred-A")
    assert service.get_for_credential(rec["task_id"], None)["task_id"] == rec["task_id"]


def test_delete_non_terminal_fails_loudly(service) -> None:
    """未终态删除必须**响亮失败** —— 上游没有取消端点。"""
    rec = service.create({"prompt": "x"}, credential="c")
    with pytest.raises(TaskNotDeletable) as ei:
        service.delete_for_credential(rec["task_id"], "c")
    assert ei.value.status_code == 400
    assert "没有取消端点" in str(ei.value)


def test_delete_terminal_ok(service, fake) -> None:
    fake.add_image(IMG, PNG_1PX)
    rec = service.create({"prompt": "x", "image": [IMG]}, credential="c")
    service.submit(rec["task_id"])
    service.poll_many(service.store.in_flight())
    out = service.delete_for_credential(rec["task_id"], "c")
    assert out["status"] == "DELETED"
    assert service.store.get(rec["task_id"]) is None


# ---------------------------------------------------------------------------
# 凭据指纹
# ---------------------------------------------------------------------------


def test_credential_fingerprint_is_stable_and_not_plaintext(service) -> None:
    fp = service.credential_of("sk-test-key")
    assert fp == service.credential_of("sk-test-key")
    assert fp != service.credential_of("sk-other")
    assert "sk-test-key" not in fp
    assert len(fp) == 32


def test_plaintext_key_never_lands_in_db(service) -> None:
    """🔴 落库的是指纹，**明文 Key 永不落库**。"""
    service.create({"prompt": "x"}, credential=service.credential_of("sk-secret-value"))
    with service.store.session() as s:
        from app.store import TaskRow
        rows = s.query(TaskRow).all()
    blob = " ".join(r.credential_id + r.request_json for r in rows)
    assert "sk-secret-value" not in blob
