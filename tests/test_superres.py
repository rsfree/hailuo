#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯 CI 同步超分 —— 零出网测试（MockTransport 同时路由 CDN / COS / CI）。

覆盖三件事：
1. `SuperRes` 客户端契约：签名 URL 形态、上传、CI GET 即返回放大字节、
   非 200 / 非 PNG 的响亮失败；
2. `build_plan` 的成本策略决策：请求高档 ⇒ 上游写死最便宜档 + 超分倍数；
3. `_enhance_result` 收口：全链路成功（产物 URL 换成 COS 签名 URL）/
   任一步失败 ⇒ 全部回退原图并留痕。
"""
from __future__ import annotations

import struct

import httpx
import pytest

from app.config import Settings
from app.errors import InvalidParameterError
from app.service import Service
from app.superres import CI_MAX_INPUT_EDGE, SuperRes, SuperResError
from app import models as m


def _png(width: int, height: int) -> bytes:
    """一个合法的 PNG（真实 IHDR 尺寸字段，内容随意）。"""
    import zlib

    def chunk(tag: bytes, body: bytes) -> bytes:
        c = struct.pack(">I", len(body)) + tag + body
        return c + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + b"\x40\x80\xc0" * (width * height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


SRC = _png(64, 64)          # "上游 1K 产物"（尺寸不重要，链路为准）
UPSCALED = _png(128, 128)   # "CI ×2 输出"


def make_sr(handler) -> SuperRes:
    return SuperRes(
        secret_id="AKIDtest", secret_key="secretkey", bucket="bkt-123",
        region="ap-singapore", timeout=5.0,
        transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# SuperRes 客户端契约
# ---------------------------------------------------------------------------


def test_configured_requires_all_parts() -> None:
    assert SuperRes(secret_id="a", secret_key="b", bucket="c",
                    region="r").configured()
    assert not SuperRes(secret_id="", secret_key="b", bucket="c",
                        region="r").configured()
    assert not SuperRes(secret_id="a", secret_key="b", bucket="",
                        region="r").configured()


def test_super_resolve_roundtrip_puts_and_gets() -> None:
    """上传 → CI GET 返回放大字节；请求形态逐项断言。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "PUT":
            assert request.url.host == "bkt-123.cos.ap-singapore.myqcloud.com"
            assert request.url.path in ("/superres/t1/0_x2.png.src",
                                        "/superres/t1/0_x2.png")
            assert "q-signature=" in request.headers.get("Authorization", ""), \
                "PUT 签名走 Authorization 头"
            return httpx.Response(201)
        assert request.method == "GET"
        q = dict(request.url.params)
        assert q["ci-process"] == "AISuperResolution"
        assert q["magnify"] == "2"
        auth = request.headers.get("Authorization", "")
        assert "q-signature=" in auth, "CI 请求签名走 Authorization 头"
        assert "ci-process" in auth.split("q-url-param-list=")[1].split("&")[0], \
            "ci-process 必须列入签名的参数清单"
        return httpx.Response(200, content=UPSCALED)

    sr = make_sr(handler)
    res = sr.super_resolve(SRC, 2, object_key="superres/t1/0_x2.png")
    assert res.width == 128 and res.height == 128 and res.magnify == 2
    assert res.data == UPSCALED
    # 🔴 CI 不写回对象 ⇒ 原图传 .src 键、结果必须传回主键（否则预签名给的是原图）
    assert [c.method for c in calls] == ["PUT", "GET", "PUT"]
    assert str(calls[0].url.path) == "/superres/t1/0_x2.png.src"
    assert str(calls[2].url.path) == "/superres/t1/0_x2.png"
    assert calls[2].content == UPSCALED, "回传的必须是放大结果"
    url = sr.presigned_get("superres/t1/0_x2.png", ttl_s=3600)
    assert url.startswith("https://bkt-123.cos.ap-singapore.myqcloud.com/")
    assert "q-sign-time=" in url
    sr.close()


def test_super_resolve_ci_error_is_loud(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(400, text="<Error><Code>Invalid</Code></Error>")

    sr = make_sr(handler)
    with pytest.raises(SuperResError, match="HTTP 400"):
        sr.super_resolve(SRC, 2, object_key="k.png")
    sr.close()


def test_super_resolve_rejects_non_png_answer() -> None:
    """CI 把错误包成 200 + XML 也必须响亮失败（魔数校验）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(200, text="<Error>oops</Error>")

    sr = make_sr(handler)
    with pytest.raises(SuperResError, match="非 PNG"):
        sr.super_resolve(SRC, 2, object_key="k.png")
    sr.close()


def test_super_resolve_validates_magnify() -> None:
    sr = make_sr(lambda r: httpx.Response(500))
    with pytest.raises(SuperResError, match="magnify"):
        sr.super_resolve(SRC, 3, object_key="k.png")
    sr.close()


def test_ci_input_budget_downscales_jpeg() -> None:
    """超预算输入：JPEG 重编码保尺寸（bfl 口径），不缩尺寸。"""
    from io import BytesIO

    from PIL import Image

    sr = make_sr(lambda r: httpx.Response(201))
    im = Image.new("RGB", (64, 64), (250, 30, 30))
    buf = BytesIO()
    im.save(buf, "PNG")
    unit = buf.getvalue()
    big = unit * (4_500_000 // len(unit) + 1)  # 拼接出超预算输入（PIL 只解第一张）
    assert len(big) > 4_500_000
    out = sr._input_image(big)
    got = Image.open(BytesIO(out))
    assert got.size == (64, 64), "保尺寸是第一优先级"
    assert len(out) <= 4_500_000
    sr.close()


def test_ci_max_input_edge_constant() -> None:
    """1920 是 bfl 2026-10-15 二分实测值 —— 改它必须有新的实测依据。"""
    assert CI_MAX_INPUT_EDGE == 1920


# ---------------------------------------------------------------------------
# build_plan：成本策略决策
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc() -> Service:
    m.install_runtime_models(list(m.FROZEN_SNAPSHOT))
    st = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t")
    st.validate()
    s = Service(st, fetch_capabilities=False)
    yield s
    s.close()
    m.install_runtime_models([])


def test_cost_strategy_2k_request_becomes_1k_plus_x2(svc: Service) -> None:
    plan, _, upstream = svc.build_plan(
        {"model": "gpt-image-2.5-flare", "prompt": "x", "resolution": "2K"})
    assert upstream == "gpt-image-2.5-flare"
    assert plan.resolution == "1K" and plan.quality == "low"
    assert plan.requested_resolution == "2K" and plan.superres_magnify == 2
    assert plan.forecast_credits == 2.0
    assert "gpt-image 成本策略" in " ".join(plan.degradations)


def test_cost_strategy_size_derives_magnify(svc: Service) -> None:
    """`size` 换算出的档位同样驱动超分倍数。"""
    plan, _, _ = svc.build_plan(
        {"model": "gpt-image-2.5-flare", "prompt": "x", "size": "4096x4096"})
    assert plan.requested_resolution == "4K" and plan.superres_magnify == 4
    assert plan.resolution == "1K"


def test_cost_strategy_1k_request_no_superres(svc: Service) -> None:
    """请求的就是 1K ⇒ 不超分、无成本策略留痕（没有任何东西被改）。"""
    plan, _, _ = svc.build_plan(
        {"model": "gpt-image-2.5-flare", "prompt": "x", "resolution": "1K"})
    assert plan.superres_magnify is None
    assert plan.resolution == "1K" and plan.quality == "low"
    assert not any("成本策略" in d for d in plan.degradations)


def test_cost_strategy_not_gpt_image_untouched(svc: Service) -> None:
    """nano 等非 gpt-image 模型不进成本策略（2K 请求仍透传上游）。"""
    plan, _, _ = svc.build_plan(
        {"model": "nano_banana21_flash", "prompt": "x", "resolution": "2K"})
    assert plan.resolution == "2K" and plan.superres_magnify is None


def test_cost_strategy_gpt15_redirect_pin(svc: Service) -> None:
    """gpt-image-1.5 已定向 flare：High ⇒ 翻译 4K ⇒ 上游 1K/low + 超分 ×4。"""
    plan, _, upstream = svc.build_plan(
        {"model": "gpt-image-1.5", "prompt": "x", "resolution": "High"})
    assert upstream == "gpt-image-2.5-flare"
    assert plan.resolution == "1K" and plan.quality == "low"
    assert plan.requested_resolution == "4K" and plan.superres_magnify == 4
    assert plan.forecast_credits == 2.0


def test_invalid_quality_still_400(svc: Service) -> None:
    """成本策略不吞校验：gpt-image 传未知 quality 仍然 400。"""
    with pytest.raises(InvalidParameterError) as ei:
        svc.build_plan(
            {"model": "gpt-image-2.5-flare", "prompt": "x", "quality": "ultra"})
    assert ei.value.param == "quality"


# ---------------------------------------------------------------------------
# _enhance_result 收口
# ---------------------------------------------------------------------------


def _sr_settings(**kw) -> Settings:
    st = Settings(
        task_db="sqlite+pysqlite:///:memory:", hailuo_token="t",
        tencent_api_key="AKIDtest|secretkey", superres_bucket="bkt-123",
        superres_region="ap-singapore", **kw)
    st.validate()
    return st


def _fake_feed(url: str):
    from app.upstream.hailuo.client import Feed
    return Feed(feed_id="f1", batch_id="b1", status=2, feed_type=1,
                create_time=1, url=url, url_no_watermark=url)


def test_enhance_result_full_pipeline() -> None:
    """全链路：下载产物 → COS PUT → CI GET → 结果 URL = COS 签名 URL。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "cdn.hailuoai.video":
            return httpx.Response(200, content=SRC,
                                  headers={"Content-Type": "image/png"})
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(200, content=UPSCALED)

    transport = httpx.MockTransport(handler)
    svc2 = Service(_sr_settings(), fetch_capabilities=False,
                   http_transport=transport)
    try:
        payload = {"data": [{"url": "https://cdn.hailuoai.video/x.png"}],
                   "width": 64, "height": 64}
        full = {"plan": {"superres_magnify": 2, "requested_resolution": "2K"}}
        out = svc2._enhance_result(
            "task1", full, [_fake_feed("https://cdn.hailuoai.video/x.png")],
            payload)
        assert out["superres"]["status"] == "ok"
        assert out["superres"]["magnify"] == 2
        assert out["width"] == 128 and out["height"] == 128
        url = out["data"][0]["url"]
        assert "bkt-123.cos.ap-singapore.myqcloud.com" in url
        assert "q-signature=" in url
        assert any(r.method == "PUT" for r in seen)
        assert any("ci-process=AISuperResolution" in str(r.url) for r in seen)
    finally:
        svc2.close()


def test_enhance_result_fallback_on_ci_failure() -> None:
    """CI 挂 ⇒ 全部回退原图 URL + fallback 留痕（不拖垮已花钱的任务）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.hailuoai.video":
            return httpx.Response(200, content=SRC,
                                  headers={"Content-Type": "image/png"})
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(500, text="CI down")

    svc2 = Service(_sr_settings(), fetch_capabilities=False,
                   http_transport=httpx.MockTransport(handler))
    try:
        original = "https://cdn.hailuoai.video/x.png"
        payload = {"data": [{"url": original}]}
        full = {"plan": {"superres_magnify": 2}}
        out = svc2._enhance_result("task1", full, [_fake_feed(original)], payload)
        assert out["superres"]["status"] == "fallback"
        assert out["data"][0]["url"] == original, "回退必须保留原图 URL"
    finally:
        svc2.close()


def test_enhance_result_fallback_when_unconfigured() -> None:
    """没配凭据 ⇒ 直接回退（不出网）。"""
    st = Settings(task_db="sqlite+pysqlite:///:memory:", hailuo_token="t")
    st.validate()
    svc2 = Service(st, fetch_capabilities=False)
    try:
        payload = {"data": [{"url": "https://cdn.hailuoai.video/x.png"}]}
        full = {"plan": {"superres_magnify": 2}}
        out = svc2._enhance_result("task1", full, [], payload)
        assert out["superres"]["status"] == "fallback"
        assert "凭据" in out["superres"]["reason"]
    finally:
        svc2.close()


def test_enhance_result_noop_without_magnify() -> None:
    """普通任务（无超分计划）⇒ payload 原样、零出网。"""
    svc2 = Service(_sr_settings(), fetch_capabilities=False,
                   http_transport=httpx.MockTransport(
                       lambda r: pytest.fail("不应出网")))
    try:
        payload = {"data": [{"url": "https://cdn.hailuoai.video/x.png"}]}
        out = svc2._enhance_result("task1", {"plan": {}}, [], payload)
        assert out == payload
        assert "superres" not in out
    finally:
        svc2.close()


def test_enhance_result_disabled_switch() -> None:
    """SUPERRES_ENABLED=0 ⇒ 不出网、留 disabled 痕。"""
    svc2 = Service(_sr_settings(superres_enabled=False), fetch_capabilities=False,
                   http_transport=httpx.MockTransport(
                       lambda r: pytest.fail("不应出网")))
    try:
        payload = {"data": [{"url": "https://cdn.hailuoai.video/x.png"}]}
        full = {"plan": {"superres_magnify": 2}}
        out = svc2._enhance_result("task1", full, [], payload)
        assert out["superres"]["status"] == "disabled"
    finally:
        svc2.close()


def test_plan_dict_carries_superres_fields(svc: Service) -> None:
    plan, _, _ = svc.build_plan(
        {"model": "gpt-image-2.5-flare", "prompt": "x", "resolution": "2K"})
    d = plan.to_dict()
    assert d["requested_resolution"] == "2K"
    assert d["superres_magnify"] == 2
