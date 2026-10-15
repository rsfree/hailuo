#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯云数据万象（CI）同步超分 —— **gpt-image 成本策略的第二段**。

## 为什么存在（2026-10-15 用户口径）

gpt-image-* 家族上游**恒按最便宜档**生成（1K+low，2 积分；见
`service._resolve_size` 的写死口径）。调用方要的更高档位（2K/4K）由本模块
把 1K 产物**超分放大**补齐：上游 4K/low=10 积分 vs 1K/low=2 积分 + CI 一次
超分（约几分钱）—— 省下的积分是硬成本，画质差异对下游消费场景可忽略
（参考 bfl 项目 `bfl2openai/upscale.py`，2026-10-15 实测口径）。

## 腾讯 CI 同步超分（bfl 实测钉死的形态，逐条照抄）

1. `PUT https://<bucket>.cos.<region>.myqcloud.com/<key>`（COS XML-API
   sha1 签名）—— 原图先落桶：CI 的处理直接作用在**桶对象**上；
2. `GET …/<key>?ci-process=AISuperResolution&magnify=N` —— **GET 即返回
   放大后的字节**，无任务、无轮询；`magnify ∈ {2,3,4}`；
3. 输入单边上限 **1920**（bfl 2026-10-15 二分实测：1792²/1920² OK，
   1949² 起 400 `width or height exceeded limit`），输出 = 输入 × magnify；
4. 结果上传回 COS，调用方拿 **24h 预签名 GET URL**（1h 的链接被 newapi
   转手就过期了——bfl 实测教训）。

## 降级纪律

未配置凭据 / 任一步失败 ⇒ **调用方回退 1K 原图并留痕**（在任务 result 的
`superres.status=fallback` + 服务日志）。静默回退 = 让人按 4K 的预期拿到
1K 还以为成功了 —— 那是本服务最忌讳的降级形态。
"""
from __future__ import annotations

import hashlib
import hmac
import struct
import time
import urllib.parse
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import httpx
from loguru import logger

#: CI 同步超分输入单边上限（bfl 2026-10-15 实测二分钉死）。
CI_MAX_INPUT_EDGE = 1920
#: 同步超分输入体积预算：超过则 JPEG 重编码**保尺寸压体积**（超分倍数
#: 作用在输入尺寸上，缩尺寸会改变输出，所以最后才考虑缩）。
_INPUT_BUDGET = 4_500_000
#: 合法放大倍数。CI 侧 {2,3,4}；本服务只按档位用到 {2,4}。
MAGNIFY_CHOICES = (2, 4)


class SuperResError(Exception):
    """CI 超分链路任一步失败。消息里带**下一步**（超分失败该往哪想）。"""


@dataclass(frozen=True)
class SuperResResult:
    """一次成功超分的产物。"""

    data: bytes
    width: int
    height: int
    magnify: int
    #: 结果对象的 COS key（预签名 URL 由调用方按需生成 —— TTL 是调用时刻的事）
    object_key: str


def _cos_signature(secret_key: str, method: str, pathname: str,
                   key_time: str, params: dict[str, str] | None = None) -> str:
    """COS XML-API 请求签名（sha1）。

    `params` 非空时按 COS 规范参与签名：k 小写排序、值 URL 编码、
    `q-url-param-list` 列参数名（分号分隔）—— `ci-process` 就是这样进签名的。
    """
    sign_key = hmac.new(secret_key.encode("utf-8"), key_time.encode("utf-8"),
                        hashlib.sha1).hexdigest()
    kv = ""
    if params:
        kv = "&".join(f"{k.lower()}={urllib.parse.quote(str(v), safe='')}"
                      for k, v in sorted(params.items()))
    format_string = f"{method.lower()}\n{pathname}\n{kv}\n\n"
    string_to_sign = (
        f"sha1\n{key_time}\n"
        + hashlib.sha1(format_string.encode("utf-8")).hexdigest()
        + "\n"
    )
    return hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"),
                    hashlib.sha1).hexdigest()


class SuperRes:
    """腾讯 CI 同步超分客户端（同步 httpx —— 协调器收口线程直接调）。

    `transport` 供测试注入 `httpx.MockTransport`（零出网）。
    """

    def __init__(
        self,
        *,
        secret_id: str,
        secret_key: str,
        bucket: str,
        region: str,
        timeout: float = 90.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            #: 🔴 显式不信环境代理：CI 域名走代理会被静默吞请求（本机实测）。
            trust_env=False,
        )

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_settings(cls, settings: Any, *,
                      transport: httpx.BaseTransport | None = None) -> "SuperRes":
        #: 显式属性访问（不走 getattr）：`test_wiring` 静态扫描"每个配置项
        #: 都有真人读"，getattr 会把配置变成假配置。
        raw = (settings.tencent_api_key or "").strip()
        sid, _, skey = raw.partition("|")
        return cls(
            secret_id=sid.strip(), secret_key=skey.strip(),
            bucket=settings.superres_bucket,
            region=settings.superres_region,
            timeout=settings.superres_timeout,
            transport=transport,
        )

    def configured(self) -> bool:
        """凭据与桶是否齐备。false ⇒ 调用方必须回退原图（并留痕）。"""
        return bool(self.secret_id and self.secret_key and self.bucket)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ COS

    def _host(self) -> str:
        return f"{self.bucket}.cos.{self.region}.myqcloud.com"

    def upload(self, data: bytes, object_key: str) -> None:
        """PUT 一个对象进桶（失败抛 `SuperResError`，消息带 HTTP 状态）。"""
        pathname = "/" + object_key.lstrip("/")
        now = int(time.time()) - 60
        key_time = f"{now};{now + 900}"
        sig = _cos_signature(self.secret_key, "put", pathname, key_time)
        auth = (f"q-sign-algorithm=sha1&q-ak={self.secret_id}"
                f"&q-sign-time={key_time}&q-key-time={key_time}"
                f"&q-header-list=&q-url-param-list=&q-signature={sig}")
        url = f"https://{self._host()}{pathname}"
        try:
            r = self._client.put(url, content=data, headers={"Authorization": auth})
        except httpx.HTTPError as e:
            raise SuperResError(f"COS 上传失败（网络）：{type(e).__name__}: {e}") from e
        if r.status_code not in (200, 201):
            raise SuperResError(
                f"COS 上传失败：HTTP {r.status_code}: {r.text[:150]}")

    def presigned_get(self, object_key: str, ttl_s: float = 86400.0) -> str:
        """一个对象的限时下载 URL（`ttl_s` 默认 24h —— bfl 教训：1h 不够转手）。"""
        pathname = "/" + object_key.lstrip("/")
        now = int(time.time()) - 60
        key_time = f"{now};{now + int(ttl_s)}"
        sig = _cos_signature(self.secret_key, "get", pathname, key_time)
        return (f"https://{self._host()}{pathname}"
                f"?q-sign-algorithm=sha1&q-ak={self.secret_id}"
                f"&q-sign-time={key_time}&q-key-time={key_time}"
                f"&q-header-list=&q-url-param-list=&q-signature={sig}")

    # ------------------------------------------------------------------ CI

    def _ci_get(self, object_key: str, magnify: int) -> httpx.Response:
        """带签名的 CI 处理请求：GET 即返回放大后的字节。"""
        pathname = "/" + object_key.lstrip("/")
        params = {"ci-process": "AISuperResolution", "magnify": magnify}
        now = int(time.time()) - 60
        key_time = f"{now};{now + 900}"
        sig = _cos_signature(self.secret_key, "get", pathname, key_time, params)
        plist = ";".join(sorted(k.lower() for k in params))
        auth = (f"q-sign-algorithm=sha1&q-ak={self.secret_id}"
                f"&q-sign-time={key_time}&q-key-time={key_time}"
                f"&q-header-list=&q-url-param-list={plist}&q-signature={sig}")
        qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                      for k, v in params.items())
        url = f"https://{self._host()}{pathname}?{qs}&{auth}"
        try:
            return self._client.get(url, headers={"Authorization": auth})
        except httpx.HTTPError as e:
            raise SuperResError(f"CI 请求失败（网络）：{type(e).__name__}: {e}") from e

    # ------------------------------------------------------------------ 主流程

    def super_resolve(self, data: bytes, magnify: int, *,
                      object_key: str) -> SuperResResult:
        """原图字节 → CI 超分 → 结果落桶 → `SuperResResult`。

        ⚠️ 结果**不再回 CDN**：调用方拿 `presigned_get(object_key)` 的 24h URL。
        """
        if magnify not in MAGNIFY_CHOICES:
            raise SuperResError(
                f"magnify 只接受 {MAGNIFY_CHOICES}，实得 {magnify}。")
        if not self.configured():
            raise SuperResError("腾讯 CI 凭据/桶未配置（TENCENT_API_KEY + SUPERRES_BUCKET）。")

        payload = self._input_image(data)
        #: 🔴 CI 的处理**不写回对象**：GET 返回放大字节，桶里仍是原图（实测
        #: 2026-10-15：处理完再下 `object_key` 拿回的是原图）。所以原图走
        #: `.src` 暂存键、**结果由本方法上传回主键** —— 调用方对主键做
        #: 预签名拿到的才是放大图。
        source_key = object_key + ".src"
        result_key = object_key
        self.upload(payload, source_key)
        r = self._ci_get(source_key, magnify)
        if r.status_code != 200:
            raise SuperResError(
                f"CI 超分失败：HTTP {r.status_code}: {r.text[:200]}")
        if r.content[:8] != b"\x89PNG\r\n\x1a\n":
            raise SuperResError(
                f"CI 超分返回非 PNG（可能是 XML 错误体）：{r.content[:120]}")

        self.upload(r.content, result_key)
        width, height = _png_size(r.content)
        return SuperResResult(data=r.content, width=width, height=height,
                              magnify=magnify, object_key=result_key)

    # ------------------------------------------------------------------ 输入准备

    def _input_image(self, data: bytes, budget: int = _INPUT_BUDGET) -> bytes:
        """同步超分的输入准备：**保尺寸、压体积**（照抄 bfl `ci_input_image`）。

        超分倍数作用在输入尺寸上 ⇒ 尺寸纹丝不动优先（JPEG 重编码），
        确实压不进预算再按比例缩（极端兜底）。
        """
        from PIL import Image  # noqa: PLC0415 —— media.py 同款：重依赖懒加载

        if len(data) <= budget:
            return data
        try:
            im = Image.open(BytesIO(data))
            im.load()
        except Exception:  # noqa: BLE001 —— 解不开就原样交由 CI 报错
            return data
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        for quality in (92, 85, 78, 70):
            buf = BytesIO()
            im.save(buf, "JPEG", quality=quality)
            if len(buf.getvalue()) <= budget:
                return buf.getvalue()
        for scale in (0.75, 0.5):
            resized = im.resize((round(im.size[0] * scale),
                                 round(im.size[1] * scale)), Image.LANCZOS)
            buf = BytesIO()
            resized.save(buf, "JPEG", quality=85)
            if len(buf.getvalue()) <= budget:
                logger.info(f"CI 输入超预算 ⇒ 已缩放 {im.size}→{resized.size}")
                return buf.getvalue()
        return buf.getvalue()


def _png_size(data: bytes) -> tuple[int, int]:
    """PNG IHDR 里的宽高（不引 PIL 依赖链的轻量读取）。"""
    return struct.unpack(">II", data[16:24])


__all__ = [
    "CI_MAX_INPUT_EDGE",
    "MAGNIFY_CHOICES",
    "SuperRes",
    "SuperResError",
    "SuperResResult",
]
