#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图生图（i2i）**全形态**实测矩阵。

🔴 **真实建任务，会扣额度** —— 必须显式 `--allow-real-submit`。

## 覆盖的形态（每条都是不同的代码路径）

| 用例 | 形态 | 覆盖路径 |
|---|---|---|
| `i2i-mixed-form` | 1 公网 URL + 1 内联 base64 | 每项独立分派（`blob_from_source`） |
| `i2i-max-refs-14` | flash 垫图**满额** 14 张 | `maxSupportImageCount` 边界 + 14 路并行 ingest |
| `i2i-pro-1k` | `nano-banana2`（pro 档） | 换模型的 i2i |
| `i2i-seedream-2k` | `seedream-5.0`（只有 2K/4K 档） | 档位非默认的模型 |
| `i2i-mj-nofs` | `mj_v7`（无分辨率档） | 无档位模型 + i2i |
| `i2i-gpt-3ref-low` | `gpt-image-1.5` + Low 档 + 3 参考 | quality 型档位 + 该模型垫图上限=3 |
| `i2i-cheap-1cr` | `image-01`（无档位、1 credit） | 最便宜模型 |

**负例**（免费，不建任务，只验拒绝语义）：

| 用例 | 期望 |
|---|---|
| `i2i-over-limit-15` | flash 15 张（超 14）⇒ `400`，信息带上限 |
| `i2i-over-limit-gpt-17` | gpt-image-1.5 定向到 flare（上限 16）⇒ 17 张 `400` |

执行模型与 `matrix.py` 一致：**Phase A 全部提交 → Phase B 单一轮询循环收口**。
结果：产物下载到 `e2e-output/i2i/`，汇总 JSON 落 `e2e-output/i2i-result.json`。
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor  # noqa: F401 —— 与 matrix 同构留用
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT_DIR = Path("e2e-output/i2i")
RESULT_JSON = Path("e2e-output/i2i-result.json")
DB = "e2e_matrix.db"
POLL_EVERY = 5.0
GLOBAL_DEADLINE = 420.0

#: 本地文件（转 base64 用）—— 覆盖"本地图片"形态
LOCAL_PNG = "e2e-output/matrix/t2i-1k-1.png"

#: 内置参考图（今日实测产物，CDN 可直取）
BUILTIN_REFS = [
    "https://cdn.hailuoai.video/moss/prod/2026-09-21-10/user/multi_chat_file/"
    "1789958465020177483-302833867771949058_1789958463.png",
    "https://cdn.hailuoai.video/moss/prod/2026-09-21-10/user/multi_chat_file/"
    "1789959286880141814-302833867771949058_1789959285.png",
    "https://cdn.hailuoai.video/moss/prod/2026-09-21-10/user/multi_chat_file/"
    "1789959341236494651-302833867771949058_1789959338.png",
]


def header(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72, flush=True)


def ref_pool(limit: int) -> list[str]:
    """参考图池：内置 3 张 + `e2e_matrix.db` 成功任务的产物（**互异**）。"""
    pool, seen = [], set()
    for u in BUILTIN_REFS:
        if u not in seen:
            seen.add(u)
            pool.append(u)
    try:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT result_json FROM tasks WHERE status='succeeded'").fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        rows = []
    for (raw,) in rows:
        try:
            data = json.loads(raw or "{}").get("data") or []
        except Exception:  # noqa: BLE001
            continue
        for d in data:
            u = str((d or {}).get("url") or "")
            if u and u not in seen:
                seen.add(u)
                pool.append(u)
            if len(pool) >= limit:
                return pool
    #: 历史产物不够就合成补足：宁可跑"合成参考图"的多参考形态，
    #: 也不要因为环境干净就在 pool[3] 上崩掉。
    if len(pool) < limit:
        pool.extend(synth_refs(limit - len(pool), start=len(pool)))
    return pool


def local_data_url() -> str:
    path = pathlib.Path(LOCAL_PNG)
    if not path.is_file():
        raise SystemExit(
            f"缺少本地样图 {LOCAL_PNG}（'本地图片'形态要用它转 base64）。\n"
            "先跑一次 `python scripts/matrix.py --allow-real-submit --cases t2i-1k`，"
            "或把任意 PNG 放到该路径。")
    raw = path.read_bytes()
    return f"data:image/png;base64,{base64.b64encode(raw).decode()}"


def synth_refs(count: int, *, start: int = 0) -> list[str]:
    """本地合成**互异**参考图（内联 data URL）。

    为什么需要：`ref_pool` 依赖内置 3 张 + 历史成功任务；新环境里历史库是空的，
    池子凑不到 15 张 ⇒ 旧实现只打印一句警告，随后 `pool[3]` 直接 IndexError。
    合成图上传免费、不走 CDN，足以补齐"张数"这条语义（`maxSupportImageCount`
    校验、fileList 顺序、多路 ingest 并行度）。
    """
    out: list[str] = []
    for i in range(count):
        k = start + i
        img = Image.new("RGB", (64, 64),
                        ((37 * k + 20) % 256, (73 * k + 40) % 256, (109 * k + 60) % 256))
        buf = BytesIO()
        img.save(buf, format="PNG")
        out.append("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
    return out


def build_cases(pool: list[str]) -> list[tuple[str, dict]]:
    """正向用例（每条都会真建任务）。"""
    data_url = local_data_url()
    return [
        ("i2i-mixed-form",
         {"model": "nano_banana21_flash", "resolution": "1K", "n": 1,
          "prompt": "融合两张参考图的元素，生成一张电影感海报",
          "image": [pool[0], data_url]}),
        ("i2i-max-refs-14",
         {"model": "nano_banana21_flash", "resolution": "1K", "n": 1,
          "prompt": "综合全部参考图的风格，生成一张统一风格的静物写生",
          "image": pool[:14]}),
        ("i2i-pro-1k",
         {"model": "nano-banana2", "resolution": "1K", "n": 1,
          "prompt": "把画面主体换成一只青铜质感的猫，保持构图与光影",
          "image": [pool[1]]}),
        ("i2i-seedream-2k",
         {"model": "seedream-5.0", "resolution": "2K", "n": 1,
          "prompt": "把画面改成水彩插画风格，保留主体轮廓",
          "image": [pool[2]]}),
        ("i2i-mj-nofs",
         {"model": "mj_v7", "n": 1,
          "prompt": "reference-based cinematic rework, dramatic rim light, 35mm",
          "image": [pool[3]]}),
        ("i2i-gpt-3ref-low",
         {"model": "gpt-image-1.5", "resolution": "Low", "n": 1,
          "prompt": "Combine the reference images into one clean product shot.",
          "image": pool[3:6]}),
        #: ❌ 已删除 `i2i-cheap-1cr`（image-01 + 参考图）：该模型的参考图模式是
        #: `subject-reference`（主体参考），本服务只实现 `image-reference` ⇒ **本地必拒**。
        #: 它和负例 `i2i-subject-ref-model` 断言的是同一件事，作为正向用例永远不可能通过。
    ]


def negative_checks(svc: object, pool: list[str], credential: str) -> list[dict]:
    """负例：**只走本地校验**（400 在受理阶段就拦下，不发上游、不计费）。"""
    from app.errors import InvalidParameterError

    out: list[dict] = []
    for name, body in (
        ("i2i-over-limit-15",
         {"model": "nano_banana21_flash", "prompt": "x", "image": pool[:15]}),
        #: ⚠️ gpt-image-1.5 会被**定向**到 gpt-image-2.5-flare（成本策略，进 degradations），
        #: 而 flare 的垫图上限是 **16** ⇒ 必须 17 张才真正越界。
        #: 旧版用 4 张（按 gpt-image-1.5 声明的 3 张算）在定向策略落地后**已失效**：
        #: 它把一个合法请求判成"未拦截"，属于用例过期，不是实现缺陷。
        ("i2i-over-limit-gpt-17",
         {"model": "gpt-image-1.5", "prompt": "x", "image": pool[:17]}),
        #: 主体参考模型（image-01）带参考图 ⇒ 必须**本地**拒绝（实测上游 code 2400052）
        ("i2i-subject-ref-model",
         {"model": "image-01", "prompt": "x", "image": [pool[0]]}),
    ):
        try:
            svc.create(body, credential=credential)  # type: ignore[attr-defined]
            out.append({"case": name, "expected": "400", "got": "被受理（❌ 未拦截）",
                        "ok": False})
        except InvalidParameterError as e:
            msg = str(e)
            #: 判据：消息必须**可执行**（说清上限/原因与怎么办），而不是一句"参数错"
            ok = any(k in msg for k in ("上限", "参考图模式", "image-reference"))
            out.append({"case": name, "expected": "400", "got": msg[:90], "ok": ok})
        except Exception as e:  # noqa: BLE001
            out.append({"case": name, "expected": "400",
                        "got": f"{type(e).__name__}: {e}"[:90], "ok": False})
    return out


def submit_case(svc: object, name: str, body: dict, credential: str) -> dict:
    entry: dict = {"case": name, "request": {"model": body.get("model"),
                                             "image_count": len(body.get("image") or []),
                                             "resolution": body.get("resolution")}}
    try:
        rec = svc.create(body, credential=credential)  # type: ignore[attr-defined]
        entry["task_id"] = rec["task_id"]
        sub = svc.submit(rec["task_id"])  # type: ignore[attr-defined]
        entry["submitted"] = bool(sub.get("submitted"))
        entry["upstream_batch_id"] = sub.get("upstream_batch_id") or ""
        if entry["submitted"]:
            print(f"  [{name}] batch={entry['upstream_batch_id']}", flush=True)
        else:
            entry["status"] = "submit_skipped"
            entry["error"] = str(sub)
    except Exception as e:  # noqa: BLE001
        entry["submitted"] = False
        entry["status"] = "submit_exception"
        entry["error"] = f"{type(e).__name__}: {e}"
        print(f"  [{name}] 提交失败：{entry['error'][:100]}", flush=True)
    return entry


def collect(svc: object, entry: dict) -> dict:
    from app.service import view  # noqa: PLC0415

    entry.pop("request", None)
    if not entry.get("submitted"):
        return entry
    full = svc.store.get_full(entry["task_id"])  # type: ignore[attr-defined]
    entry["status"] = full["status"]
    code, payload = view(full)
    entry["http"] = code
    if full["status"] == "succeeded":
        entry["images"] = len(payload.get("data") or [])
        entry["urls"] = [d.get("url", "") for d in (payload.get("data") or [])]
        entry["usage"] = payload.get("usage")
        entry["degradations"] = payload.get("degradations") or []
    else:
        entry["error"] = (full.get("error") or {}).get("message", "")
    return entry


_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")   #: PNG / JPEG


def _looks_like_image(raw: bytes) -> bool:
    """魔术字节判定。CDN 出错时会回 HTML（实测 4KB 的 `<html>` 被存成 .png），
    把错误页当产物 = 静默假成功，比下载失败严重得多。"""
    if raw.startswith(_IMAGE_MAGIC[0]) or raw.startswith(_IMAGE_MAGIC[1]):
        return True
    return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def _suffix(raw: bytes) -> str:
    """按魔术字节定扩展名：上游产物可能是 **JPEG**（seedream 实测 2816×1536 .jpg），
    统一存成 .png 会让下游按错误类型处理。"""
    if raw.startswith(_IMAGE_MAGIC[0]):
        return ".png"
    if raw.startswith(_IMAGE_MAGIC[1]):
        return ".jpg"
    return ".webp"


def _get_retry(url: str, *, attempts: int = 4) -> Any:
    """带退避的 GET。**CDN 偶发掐新建连接**（实测 t2i-1k 首次 ConnectTimeout），
    下载是幂等的 ⇒ 重试零副作用；5xx/429 也重试，4xx 直接放弃（重试无意义）。
    """
    import httpx  # noqa: PLC0415

    last = ""
    for i in range(1, attempts + 1):
        try:
            r = httpx.get(url, timeout=30, follow_redirects=True)
            if r.status_code in (429,) or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
            else:
                return r
        except httpx.TransportError as e:
            last = f"{type(e).__name__}: {e}"
        if i < attempts:
            time.sleep(0.4 * i)
    raise RuntimeError(f"下载失败（已重试 {attempts} 次）：{url}（{last}）")


def download(name: str, idx: int, url: str) -> str:
    import httpx  # noqa: PLC0415

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    r = _get_retry(url)
    raw = r.content
    if r.status_code != 200 or not _looks_like_image(raw):
        raise RuntimeError(
            f"产物不是图片（HTTP {r.status_code}, {len(raw)}B, "
            f"content-type={r.headers.get('content-type')}）：{url}")
    path = OUT_DIR / f"{name}-{idx}{_suffix(raw)}"
    path.write_bytes(raw)
    return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="i2i 全形态矩阵（🔴 计费）")
    ap.add_argument("--allow-real-submit", action="store_true")
    ap.add_argument("--cases", default="", help="逗号分隔的子集；默认全跑")
    ap.add_argument("--negatives-only", action="store_true",
                    help="只跑负例校验（**零计费**：全在本地拦截）")
    args = ap.parse_args(argv)

    from app.config import Settings  # noqa: PLC0415
    from app.service import Service  # noqa: PLC0415

    st = Settings.from_env().replace(task_db=f"sqlite+pysqlite:///./{DB}")
    svc = Service(st, fetch_capabilities=False)
    #: 🔴 透传鉴权：脚本用自己的 HAILUO_TOKEN 登记一个凭据
    credential = svc.register_credential(st.hailuo_token)
    results: list[dict] = []
    try:
        pool = ref_pool(limit=20)  # ⚠️ 必须 >14：超限负例要凑够 15 张才有意义
        real = sum(1 for u in pool if u.startswith("http"))
        synth = len(pool) - real
        print(f"  参考图池 {len(pool)} 张：真实 {real} + 合成 {synth}")
        #: 用例里最深的下标是 pool[4] 与 pool[3:6] ⇒ 少于 6 张连构造都会崩。
        assert len(pool) >= 6, f"参考图池只有 {len(pool)} 张，不足以构造用例" 
        cases = build_cases(pool)
        if args.cases:
            want = {c.strip() for c in args.cases.split(",") if c.strip()}
            cases = [(n, b) for n, b in cases if n in want]

        header(f"i2i 全形态矩阵 —— {len(cases)} 个正向用例（🔴 计费）+ 3 个负例（免费）")
        for name, body in cases:
            print(f"  · {name}: model={body.get('model')} "
                  f"refs={len(body.get('image') or [])} "
                  f"res={body.get('resolution') or '（默认/无档）'}")

        if not args.allow_real_submit and not args.negatives_only:
            print("\nDRAFT（未开闸）—— 不花一分钱。确认执行请加 --allow-real-submit")
            return 0

        # ---- 负例（免费，不发上游）
        header("负例校验（本地拦截，不发上游）")
        negatives = negative_checks(svc, pool, credential)
        for n in negatives:
            print(f"  {'✅' if n['ok'] else '❌'} {n['case']:<24} {n['got']}")
        if args.negatives_only:
            neg_ok = sum(1 for n in negatives if n["ok"])
            print(f"\n负例 {neg_ok}/{len(negatives)} 正确拦截（零计费）")
            return 0 if neg_ok == len(negatives) else 1

        # ---- Phase A：提交
        header("Phase A —— 提交正向用例")
        entries = []
        for i, (name, body) in enumerate(cases, 1):
            print(f"\n—— 提交 {i}/{len(cases)} {name} ——", flush=True)
            entries.append(submit_case(svc, name, body, credential))
        in_flight = {e["task_id"] for e in entries if e.get("submitted")}
        print(f"\n已提交 {len(in_flight)}/{len(entries)}，进入并行收口", flush=True)

        # ---- Phase B：共享轮询
        header("Phase B —— 共享轮询（每轮合并 1 次上游查询）")
        deadline = time.time() + GLOBAL_DEADLINE
        t0 = time.time()
        while in_flight and time.time() < deadline:
            svc.poll_many(svc.store.in_flight())
            done = {tid for tid in in_flight
                    if (svc.store.get(tid) or {}).get("status") in ("succeeded", "failure")}
            for tid in done:
                print(f"  · {tid} → {(svc.store.get(tid) or {}).get('status')}"
                      f"（{round(time.time() - t0, 1)}s）", flush=True)
            in_flight -= done
            if in_flight:
                time.sleep(POLL_EVERY)
        if in_flight:
            print(f"  ⏱ 全局超时仍未终态：{in_flight}", flush=True)

        # ---- 收结果 + 下载
        header("结果")
        for e in entries:
            r = collect(svc, e)
            results.append(r)
            print(f"  {'✅' if r.get('status') == 'succeeded' else '❌'} "
                  f"{r['case']:<20} {r.get('status', '?'):<12} images={r.get('images', 0)}")
            if r.get("error"):
                print(f"      {r['error'][:110]}")
        header("产物下载")
        for r in results:
            for j, url in enumerate(r.get("urls") or [], 1):
                try:
                    p = download(r["case"], j, url)
                    print(f"  ✅ {r['case']}#{j} → {p}")
                except Exception as e:  # noqa: BLE001
                    print(f"  ❌ {r['case']}#{j} 下载失败：{e}")

        ok = sum(1 for r in results if r.get("status") == "succeeded")
        neg_ok = sum(1 for n in negatives if n["ok"])
        print(f"\n正向 {ok}/{len(results)} 成功；负例 {neg_ok}/{len(negatives)} 正确拦截")
        RESULT_JSON.parent.mkdir(parents=True, exist_ok=True)
        RESULT_JSON.write_text(json.dumps(
            {"positive": results, "negative": negatives}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"明细：{RESULT_JSON}")
        return 0 if (ok == len(results) and neg_ok == len(negatives)) else 1
    finally:
        from app.observability import OBS  # noqa: PLC0415

        OBS.flush()
        svc.close()


if __name__ == "__main__":
    raise SystemExit(main())
