#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端探针 —— **默认零成本**，花钱要显式开闸。

```
# 全部零成本阶段（默认）：签名自证 + 契约自检 + 受理 + 上游只读
python scripts/e2e.py

# 加上真实上传（免费，但会在你账号里留一个素材）
python scripts/e2e.py --phases sign,capabilities,accept,dry,upload --allow-upload

# 🔴 真实建任务（**扣额度**）—— 必须显式二次确认，默认永不执行
python scripts/e2e.py --phases generate --allow-real-submit
```

| 阶段 | 花额度 | 做什么 |
|---|---|---|
| `sign` | ❌ | 5 条抓包向量离线复算（**纯本地，不出网**） |
| `capabilities` | ❌ | 读两个公开配置端点，打印实读到的 11 个模型 |
| `probe` | ❌ | 用 `my/processing` 做一次凭据自检（不建任务） |
| `accept` | ❌ | 起服务 + `POST` 受理并断言**只回一个 task_id**（协调器关 ⇒ 零上游往返） |
| `dry` | ❌ | `submit(dry_run=True)`：走完下载 + 归一化 + **签名 + body 构造**，**不发请求** |
| `upload` | ❌ | 真打上游跑 OSS 三段式（免费；需 `--allow-upload`） |
| `generate` | 🔴 | **真建任务 + 轮询到终态**（需 `--allow-real-submit`） |

🔴 **`generate` 单独隔开**，因为建任务是**计费动作**。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 合成 JWT：透传鉴权用（**零真实凭据** —— 服务零出网，任何真实调用都不会发生）
from tests.conftest import FAKE_JWT  # noqa: E402

#: 本地探针的临时任务库（不碰生产库）
PROBE_DB = os.environ.get("E2E_DB", "sqlite+pysqlite:///./e2e_probe.db")

OK = "  ✅"
NO = "  ❌"
SKIP = "  ⏭️ "


def header(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def build_settings(*, coordinator: bool = False, token: str | None = None):
    from app.config import Settings

    os.environ.setdefault("TASK_DB", PROBE_DB)
    st = Settings.from_env().replace(
        task_db=PROBE_DB,
        coordinator_enabled=coordinator,
        otel_token="",                     # 探针不上报
    )
    if token is not None:
        st = st.replace(hailuo_token=token)
    st.validate()
    return st


# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------


def phase_sign() -> bool:
    """离线复算 5 条抓包向量。**纯本地，不出网。**"""
    header("① sign —— 抓包向量离线复算（不出网）")
    from app.upstream.hailuo import sign

    bad = 0
    for v in sign.VECTORS:
        got = sign.sign_yy(path_with_query=v.path_with_query, body_json=v.body_json,
                           time_ms=v.unix_ms, method=v.method)
        good = got == v.expect_yy
        bad += not good
        print(f"{OK if good else NO} {v.name}")
        if not good:
            print(f"       expect={v.expect_yy}\n       got   ={got}")
    print(f"\n  {len(sign.VECTORS) - bad}/{len(sign.VECTORS)} 条复算通过")
    return bad == 0


def phase_capabilities(token: str) -> bool:
    """读两个**公开、免鉴权**的配置端点（零成本）。"""
    header("② capabilities —— 上游能力表（公开端点，零成本）")
    import httpx

    from app.upstream.hailuo import capabilities as caps

    base = os.environ.get("HAILUO_BASE_URL", "https://hailuoai.video")
    with httpx.Client(base_url=base, timeout=20.0) as c:
        for label, path, parser in (
            ("common_config", caps.COMMON_CONFIG_PATH, caps.parse_common_config),
            ("model/info", caps.MODEL_INFO_PATH, caps.parse_model_info),
        ):
            try:
                r = c.get(path)
                r.raise_for_status()
                parsed = parser(r.json())
                print(f"{OK} {label:<14} http={r.status_code} 条目={len(parsed)}")
            except Exception as e:  # noqa: BLE001
                print(f"{NO} {label:<14} {type(e).__name__}: {e}")
                return False

        # 合并后打印注册表
        meta = caps.parse_common_config(c.get(caps.COMMON_CONFIG_PATH).json())
        info = caps.parse_model_info(c.get(caps.MODEL_INFO_PATH).json())
        merged = caps.merge(meta, info)

    print(f"\n  合计 {len(merged)} 个图片模型：")
    print(f"  {'modelID':<26}{'裸图上限':<10}{'分辨率':<18}{'单价'}")
    print(f"  {'-' * 68}")
    for m in merged:
        costs = ", ".join(
            f"{'/'.join(r.get('resolutions') or [])}:{r.get('realCost')}"
            + (f"({','.join(r.get('qualities') or [])})" if r.get("qualities") else "")
            for r in m.costs) or (f"default={m.default_cost}" if m.default_cost else "—")
        print(f"  {m.model_id:<26}{str(m.max_support_image_count):<10}"
              f"{'/'.join(m.resolutions) or '—':<18}{costs}")
    print(f"\n  覆盖度：{caps.coverage(merged)}")
    return bool(merged)


def phase_probe(token: str) -> bool:
    """凭据自检：查一次 `my/processing`（**不建任务、零成本**）。"""
    header("③ probe —— 凭据自检（不建任务）")
    from app.upstream.hailuo.client import HailuoClient

    st = build_settings(token=token)
    c = HailuoClient(token=st.hailuo_token, base_url=st.hailuo_base_url,
                     device=st.device_profile())
    try:
        ok, why = c.probe_token()
        print(f"{OK if ok else NO} {why}")
        return ok
    finally:
        c.close()


def phase_accept(token: str) -> bool:
    """起真实服务（**协调器关**）→ 受理 → 断言只回一个 task_id。"""
    header("④ accept —— 受理契约（协调器关 ⇒ 零上游往返）")
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.service import Service

    st = build_settings(token=token, coordinator=False)
    svc = Service(st, fetch_capabilities=True)
    from tests.conftest import FAKE_JWT

    app = create_app(st, service=svc)
    #: 入口鉴权**只有两种**：hailuo JWT 透传，或 SERVICE_KEYS 里的服务 Key。
    #: 有真 token 就用它（顺带验证真凭据能过本地校验），没有就退回合成 JWT。
    key = token or FAKE_JWT
    svc.register_credential(key)
    client = TestClient(app)

    r = client.post("/async/v1/images/generations",
                    json={"model": "hailuo-i2i", "prompt": "a cat",
                          "image": ["https://cdn.hailuoai.video/e2e-ref.png"]},
                    headers={"Authorization": f"Bearer {key}"})
    body = r.json()
    ok = r.status_code == 202 and set(body) == {"task_id"}
    print(f"{OK if ok else NO} POST -> {r.status_code} {body}")
    print(f"     Location: {r.headers.get('Location')}")

    r2 = client.get(f"/async/v1/images/generations/{body.get('task_id', 'x')}")
    ok2 = r2.status_code == 202
    print(f"{OK if ok2 else NO} GET  -> {r2.status_code} {r2.json()}")

    r3 = client.get("/async/v1/models")
    print(f"{OK if r3.status_code == 200 else NO} GET /async/v1/models -> "
          f"{r3.status_code}, {len(r3.json().get('data', []))} 项")

    r4 = client.get("/readyz")
    print(f"{OK if r4.status_code == 200 else NO} GET /readyz -> "
          f"{r4.status_code} {r4.json()}")

    OBS = __import__("app.observability", fromlist=["OBS"]).OBS
    OBS.flush()
    svc.close()
    return bool(ok and ok2)


def phase_dry(token: str, image: str) -> bool:
    """草稿预演：走完整条**翻译链**，**一个字节都不发**。

    刻意**不依赖外部图片 URL 可达**：
      · 用 `build_plan()`（纯函数）验证参数翻译与降级留痕；
      · 用 `create_image(dry_run=True)` 验证签名、查询串与 body 构造；
      · `fileList` 用 `UploadedFile.to_file_list_entry()` 真实构造，
        形状与抓包一致；
      · 另外**顺带**试一次真实下载 + 归一化（纯只读），
        成功与否只作信息展示 —— 探针不该因为一张图挂掉而失去全部价值。
    """
    header("⑤ dry —— 草稿预演（签名 + body 全构造，不发请求）")
    from app.media import sniff_mime
    from app.service import Service
    from app.upstream.hailuo.upload import UploadedFile
    from tests.conftest import _make_png  # type: ignore[import-not-found]

    st = build_settings(token=token)
    svc = Service(st, fetch_capabilities=True)
    try:
        # --- ① 翻译层（纯函数）：参数怎么变成上游字段、哪里留了痕
        plan, capability, upstream_model = svc.build_plan(
            {"model": "hailuo-i2i", "prompt": "a cat", "image": [image],
             "n": 1, "size": "4096x4096"})
        print(f"{OK} build_plan：能力={capability} 上游模型={upstream_model}")
        print(f"     计划：{json.dumps(plan.to_dict(), ensure_ascii=False, indent=6)}")
        print(f"     预估积分：{plan.forecast_credits}")
        print("     降级说明（**静默降级等于让人按 A 的预期为 B 付钱**）：")
        for d in plan.degradations:
            print(f"       · {d}")

        # --- ② 签名 + body 构造（零发送）
        entry = UploadedFile(file_id="<probe>", url=image, name="probe.png",
                             file_type="png").to_file_list_entry()
        _, trace = svc.client.create_image(
            model_id=plan.upstream_model, desc=plan.desc, file_list=[entry],
            quantity=plan.quantity, aspect_ratio=plan.aspect_ratio,
            resolution=plan.resolution, quality=plan.quality,
            reference_mode=plan.reference_mode, dry_run=True)
        body = json.loads(trace["request_body"])
        print(f"\n{OK} 上游 body（**未发送**）："
              f"{json.dumps(body, ensure_ascii=False, indent=6)}")
        print(f"     yy 签名：{trace['yy']}")
        print(f"     fileList entry：{json.dumps(entry, ensure_ascii=False)}")

        # --- ③ 顺带验证媒体层（只读，失败只提示）
        try:
            from app.media import download_image, normalize

            blob = download_image(image, max_bytes=st.max_download_bytes)
            blob, notes = normalize(blob, max_side=st.normalize_max_side,
                                    max_bytes=st.normalize_max_bytes,
                                    enabled=st.normalize_uploads)
            print(f"\n{OK} 输入图：{blob.mime} {blob.size}B "
                  f"{blob.width}×{blob.height}（归一化说明 {len(notes)} 条）")
            for n in notes:
                print(f"       · {n}")
        except Exception as e:  # noqa: BLE001
            probe_png = _make_png(64, 64)
            #: ⚠️ 别把带反斜杠的字节字面量写进 f-string —— Python 3.11 是**语法错误**
            #: （3.12 才放开）。CI 的 3.11 矩阵会直接炸，所以先算好再插值。
            sniff = sniff_mime(probe_png)
            sniff_txt = f"{sniff[0]} / {sniff[1]}" if sniff else "认不出"
            print(f"\n  ⚠️  给定 URL 取不到（{type(e).__name__}: {e}）。")
            print(f"     媒体层改用本地生成的 {len(probe_png)}B PNG 自证："
                  f"魔数嗅探 → {sniff_txt}")
            print("     （这一步不影响上面的签名与 body 结论）")

        return True
    finally:
        from app.observability import OBS

        OBS.flush()
        svc.close()


def phase_upload(token: str) -> bool:
    """真打上游跑 OSS 三段式（**免费**，但会在账号里留一个素材）。"""
    header("⑥ upload —— OSS 三段式真实上传（免费）")
    from app.media import from_bytes
    from app.service import Service
    from tests.conftest import _make_png  # type: ignore[import-not-found]

    st = build_settings(token=token)
    svc = Service(st, fetch_capabilities=True)
    try:
        blob = from_bytes(_make_png(64, 64), name="e2e-probe.png")
        uploaded, traces = svc.uploader.upload_bytes(
            content=blob.data, mime=blob.mime, name=blob.name)
        for t in traces:
            stage = t.get("stage", "?")
            print(f"{OK} {stage:<18} {json.dumps({k: v for k, v in t.items() if k != 'stage'}, ensure_ascii=False)[:120]}")
        print(f"\n  fileID = {uploaded.file_id}")
        print(f"  url    = {uploaded.url}")
        print(f"  fileList entry = {json.dumps(uploaded.to_file_list_entry(), ensure_ascii=False)}")
        return bool(uploaded.file_id)
    finally:
        from app.observability import OBS

        OBS.flush()
        svc.close()


def phase_generate(token: str, image: str) -> bool:
    """🔴 真实建任务 + 轮询到终态。**会扣额度。**

    `image` 传空串 = **纯文生图**（t2i，无垫图、零上传）；
    给 URL = 图生图（i2i，提交前会真实上传垫图）。
    """
    header("⑦ generate —— 🔴 真实建任务（计费！）")
    from app.service import Service

    st = build_settings(token=token)
    svc = Service(st, fetch_capabilities=True)
    try:
        image = (image or "").strip()
        payload = {"model": "nano_banana21_flash",
                   "prompt": "a red cube on white", "n": 1,
                   "resolution": "1K"}
        mode = "文生图(t2i)"
        if image:
            payload["image"] = [image]
            mode = "图生图(i2i)"
        print(f"  模式：{mode}  model=nano_banana21_flash  resolution=1K  n=1")
        #: 真实 token ⇒ 必须**先登记进池**再提交：脚本直连 Service 时没有 HTTP
        #: 鉴权层替我们 `register_credential()`，池里查不到该指纹会抛
        #: `CredentialUnavailable`（503 语义，不是 401）。
        credential = (svc.register_credential(token) if token
                      else svc.credential_of(FAKE_JWT))
        rec = svc.create(payload, credential=credential)
        print(f"  受理 {rec['task_id']}，正在建任务…")
        out = svc.submit(rec["task_id"])
        print(f"{OK if out.get('submitted') else NO} {out.get('upstream_batch_id') or out}")

        import time
        deadline = time.time() + 180
        while time.time() < deadline:
            svc.poll_many(svc.store.in_flight())
            full = svc.store.get_full(rec["task_id"])
            print(f"     status={full['status']} upstream={full['upstream_status']}")
            if full["status"] in ("succeeded", "failure"):
                from app.service import view
                print(json.dumps(view(full)[1], ensure_ascii=False, indent=6))
                return full["status"] == "succeeded"
            time.sleep(5)
        print(f"{NO} 超时（任务可能仍在上游跑并计费）")
        return False
    finally:
        from app.observability import OBS

        OBS.flush()
        svc.close()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

DEFAULT_PHASES = "sign,capabilities,probe,accept,dry"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="hailuo-service 端到端探针（默认零成本）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phases", default=DEFAULT_PHASES,
                    help=f"逗号分隔。默认 {DEFAULT_PHASES}；可选 "
                         f"sign,capabilities,probe,accept,dry,upload,generate")
    ap.add_argument("--token", default=os.environ.get("HAILUO_TOKEN", ""),
                    help="hailuo 的 token（默认读 HAILUO_TOKEN）")
    ap.add_argument("--image", default="https://cdn.hailuoai.video/e2e-ref.png",
                    help="垫图 URL；传空串 = 纯文生图（t2i，无上传）")
    ap.add_argument("--allow-upload", action="store_true",
                    help="允许真实上传（免费，但会在你账号里留一个素材）")
    ap.add_argument("--allow-real-submit", action="store_true",
                    help="🔴 允许真实建任务（**扣额度**）")
    args = ap.parse_args(argv)

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]

    # ---- 开闸检查：把"会不会花钱/改动账号"讲清楚
    if "generate" in phases and not args.allow_real_submit:
        print("🔴 阶段 generate 会**真实建任务并扣额度**。\n"
              "   确认要跑请显式加 --allow-real-submit。\n"
              "   已跳过 generate。")
        phases = [p for p in phases if p != "generate"]
    if "upload" in phases and not args.allow_upload:
        print("⚠️  阶段 upload 会在你的 hailuo 账号里留下一个素材（免费）。\n"
              "   确认要跑请显式加 --allow-upload。\n"
              "   已跳过 upload。")
        phases = [p for p in phases if p != "upload"]

    # `capabilities` **不需要 token**：它读的两个端点是公开、免鉴权的
    # （实测不带 token/yy 也 200）⇒ 把它排除在外，让探针能在没凭据时先跑通这一层。
    needs_token = {"probe", "accept", "dry", "upload", "generate"}
    if set(phases) & needs_token and not args.token:
        print("缺少 token：请设 HAILUO_TOKEN 或传 --token。\n"
              "（sign 阶段不需要 token，可单独跑：--phases sign）")
        phases = [p for p in phases if p not in needs_token]

    print(f"将执行阶段：{phases or '（无）'}")
    if "generate" in phases:
        print("🔴 其中 generate 会消耗额度。")

    results: dict[str, bool] = {}
    for p in phases:
        try:
            if p == "sign":
                results[p] = phase_sign()
            elif p == "capabilities":
                results[p] = phase_capabilities(args.token)
            elif p == "probe":
                results[p] = phase_probe(args.token)
            elif p == "accept":
                results[p] = phase_accept(args.token)
            elif p == "dry":
                results[p] = phase_dry(args.token, args.image)
            elif p == "upload":
                results[p] = phase_upload(args.token)
            elif p == "generate":
                results[p] = phase_generate(args.token, args.image)
            else:
                print(f"{SKIP} 未知阶段 {p}")
        except Exception as e:  # noqa: BLE001
            print(f"{NO} 阶段 {p} 抛异常：{type(e).__name__}: {e}")
            results[p] = False

    header("汇总")
    for k, v in results.items():
        print(f"{OK if v else NO} {k}")
    failed = [k for k, v in results.items() if not v]
    print(f"\n{'全部通过' if not failed else '失败：' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
