#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片生成**全形态**实测矩阵（并行版）。

🔴 **真实建任务，会扣额度** —— 必须显式 `--allow-real-submit`，否则只打印计划。

## 执行模型（对齐本服务的批量轮询架构）

**Phase A**：把全部用例**依次提交**到上游（受理+建任务，秒级）；
**Phase B**：**一个共享轮询循环**推全部在途任务 —— `poll_many` 每轮 tick
把所有在途任务合并成 1 次上游查询（+必要的 v4 兜底），与用例数无关。
墙钟时间 ≈ 最慢的用例，而不是各用例之和。

## 形态设计原则：每个用例覆盖一条不同的代码路径

| 用例 | 形态 | 覆盖路径 |
|---|---|---|
| t2i-1k | 文生图 1K | 全新 submit→poll 全链路基线 |
| i2i-single | 图生图单参考 | 输入图下载→归一化→OSS 上传→fileList 编辑语义 |
| i2i-multi-3ref | 图生图三参考 | fileList 顺序多图（maxSupportImageCount 校验） |
| t2i-n2 | 文生图 n=2 | quantity>1：一个 batch 多条 feed、**产物聚合收口** |
| t2i-2k-916 | 文生图 2K+9:16 | resolution / aspect_ratio 原生档透传 |

## 参考图来源（按优先级）

1. `e2e-output/matrix-result.json` 里**上一轮已验证可下载**的产物 URL；
2. 兜底：`my/batch` 只读扫描历史产物（网络抖动风险自担）。
上传免费，没必要为凑参考图多花钱。

结果：成功产物下载到 `e2e-output/matrix/`；汇总 JSON 落
`e2e-output/matrix-result.json`；任务落 `e2e_matrix.db`（事后可单独重查）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT_DIR = Path("e2e-output/matrix")
RESULT_JSON = Path("e2e-output/matrix-result.json")
MODEL = "nano_banana21_flash"
POLL_EVERY = 5.0
#: 共享轮询的全局死线 —— 覆盖"5 个并行用例里最慢的 2K"（实测 ~96s）再放宽
GLOBAL_DEADLINE = 420.0

CUBE_URL = ("https://cdn.hailuoai.video/moss/prod/2026-09-21-10/user/multi_chat_file/"
            "1789958465020177483-302833867771949058_1789958463.png")


def header(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72, flush=True)


def _prev_urls() -> list[str]:
    """上一轮矩阵成功产物 URL（已验证可下载 ⇒ 当参考图最稳）。"""
    try:
        data = json.loads(RESULT_JSON.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    urls: list[str] = []
    for case in data:
        for u in case.get("urls") or []:
            if u and u not in urls:
                urls.append(u)
    return urls


def collect_history_refs(client: Any, want: int = 2) -> list[str]:
    """只读扫描 `my/batch`，取历史上的图片产物 URL 当参考图。"""
    batches, _ = client.fetch_batches(limit=30)
    refs: list[str] = []
    for _batch_id, feeds in batches:
        for f in feeds:
            if f.url and f.url not in refs:
                refs.append(f.url)
            if len(refs) >= want:
                return refs
    return refs


def build_cases(svc: Any) -> list[tuple[str, dict]]:
    """构造用例。i2i 的参考图现场收集（产物复用优先，历史扫描兜底）。"""
    refs: list[str] = []
    for u in _prev_urls():
        if u != CUBE_URL and u not in refs:
            refs.append(u)
        if len(refs) >= 2:
            break
    if len(refs) < 2 and svc.settings.upstream_configured:
        try:
            from app.upstream.hailuo.client import HailuoClient  # noqa: PLC0415

            c = HailuoClient(token=svc.settings.hailuo_token,
                             base_url=svc.settings.hailuo_base_url,
                             device=svc.settings.device_profile())
            for u in collect_history_refs(c, want=2 - len(refs)):
                if u not in refs:
                    refs.append(u)
            c.close()
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 历史参考图获取失败（{type(e).__name__}: {e}）")

    cases: list[tuple[str, dict]] = [
        ("t2i-1k", {"model": MODEL, "prompt": "a green pyramid on golden sand, clear blue sky",
                    "resolution": "1K", "n": 1}),
        ("i2i-single", {"model": MODEL, "prompt": "把这颗立方体换成蓝色金属质感，保持构图不变",
                        "image": [CUBE_URL], "resolution": "1K", "n": 1}),
        ("t2i-n2", {"model": MODEL, "prompt": "a purple glossy sphere on white background",
                    "resolution": "1K", "n": 2}),
        ("t2i-2k-916", {"model": MODEL, "prompt": "a yellow star glowing in the night sky",
                        "resolution": "2K", "aspect_ratio": "9:16", "n": 1}),
    ]
    if len(refs) >= 2:
        cases.insert(2, ("i2i-multi-3ref", {
            "model": MODEL, "prompt": "融合参考图的元素，生成一个复古机器人玩具的产品图",
            "image": [CUBE_URL] + refs[:2], "resolution": "1K", "n": 1}))
    else:
        print(f"  ⚠️ 参考图不足（{len(refs)}/2）⇒ i2i-multi-3ref 本轮跳过")
    return cases


def submit_case(svc: Any, name: str, body: dict, credential: str) -> dict:
    """受理 + 建任务（计费动作）。失败不中断矩阵（记录后继续下一个）。"""
    out: dict = {"case": name, "request": body}
    try:
        rec = svc.create(body, credential=credential)
        out["task_id"] = rec["task_id"]
        sub = svc.submit(rec["task_id"])
        out["submitted"] = bool(sub.get("submitted"))
        out["upstream_batch_id"] = sub.get("upstream_batch_id") or ""
        if out["submitted"]:
            print(f"  [{name}] batch={out['upstream_batch_id']}", flush=True)
        else:
            out["status"] = "submit_skipped"
            out["error"] = str(sub)
    except Exception as e:  # noqa: BLE001
        out["submitted"] = False
        out["status"] = "submit_exception"
        out["error"] = f"{type(e).__name__}: {e}"
        print(f"  [{name}] 提交失败：{out['error']}", flush=True)
    return out


def collect_result(svc: Any, entry: dict) -> dict:
    """从库里取该用例的终态与产物（不做任何上游调用）。"""
    from app.service import view  # noqa: PLC0415

    entry.pop("request", None)
    if not entry.get("submitted"):
        return entry
    full = svc.store.get_full(entry["task_id"])
    entry["status"] = full["status"]
    entry["upstream_status"] = full.get("upstream_status")
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


def download(name: str, idx: int, url: str) -> str:
    import httpx  # noqa: PLC0415

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    r = httpx.get(url, timeout=30, follow_redirects=True)
    raw = r.content
    if r.status_code != 200 or not _looks_like_image(raw):
        raise RuntimeError(
            f"产物不是图片（HTTP {r.status_code}, {len(raw)}B, "
            f"content-type={r.headers.get('content-type')}）：{url}")
    path = OUT_DIR / f"{name}-{idx}{_suffix(raw)}"
    path.write_bytes(raw)
    return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全形态实测矩阵（🔴 计费，并行收口）")
    ap.add_argument("--allow-real-submit", action="store_true",
                    help="🔴 允许真实建任务（扣额度）")
    ap.add_argument("--cases", default="",
                    help="逗号分隔的用例名子集；默认全跑")
    args = ap.parse_args(argv)

    from app.config import Settings  # noqa: PLC0415
    from app.service import Service  # noqa: PLC0415

    st = Settings.from_env().replace(task_db="sqlite+pysqlite:///./e2e_matrix.db")
    svc = Service(st, fetch_capabilities=True)
    #: 🔴 透传鉴权：脚本用自己的 HAILUO_TOKEN 登记一个凭据，任务记在它头上
    credential = svc.register_credential(st.hailuo_token)
    results: list[dict] = []
    try:
        cases = build_cases(svc)
        if args.cases:
            wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
            cases = [(n, b) for n, b in cases if n in wanted]

        if not args.allow_real_submit:
            header("DRAFT（未开闸）—— 以下为将要执行的矩阵，不花一分钱")
            for name, body in cases:
                print(f"  · {name}: {json.dumps(body, ensure_ascii=False)[:160]}")
            print("\n执行模型：全部提交 → 单一轮询循环并行收口")
            print("确认执行请加 --allow-real-submit")
            return 0

        # ---- Phase A：全部提交
        header(f"Phase A —— 提交 {len(cases)} 个用例（🔴 计费）")
        entries: list[dict] = []
        for i, (name, body) in enumerate(cases, 1):
            print(f"\n—— 提交 {i}/{len(cases)} {name} ——", flush=True)
            entry = submit_case(svc, name, body, credential)  # type: ignore[arg-type]
            entries.append(entry)
        in_flight_ids = {e["task_id"] for e in entries if e.get("submitted")}
        print(f"\n已提交 {len(in_flight_ids)}/{len(entries)} 个任务，进入并行收口", flush=True)

        # ---- Phase B：单一轮询循环，推全部在途任务
        header("Phase B —— 共享轮询（poll_many 每轮合并为 1 次上游查询）")
        deadline = time.time() + GLOBAL_DEADLINE
        while in_flight_ids and time.time() < deadline:
            svc.poll_many(svc.store.in_flight())
            done: set[str] = set()
            for tid in in_flight_ids:
                st_now = (svc.store.get(tid) or {}).get("status")
                if st_now in ("succeeded", "failure"):
                    done.add(tid)
                    print(f"  · {tid} → {st_now}（{round(time.time() - (deadline - GLOBAL_DEADLINE), 1)}s）",
                          flush=True)
            in_flight_ids -= done
            if in_flight_ids:
                time.sleep(POLL_EVERY)
        if in_flight_ids:
            print(f"  ⏱ 全局超时，仍未终态：{in_flight_ids}（任务可能仍在上游跑并计费）", flush=True)

        # ---- 收结果
        header("结果")
        for entry in entries:
            results.append(collect_result(svc, entry))
            r = results[-1]
            print(f"  {'✅' if r.get('status') == 'succeeded' else '❌'} "
                  f"{r['case']:<16} {r.get('status', '?'):<12} images={r.get('images', 0)}")
            if r.get("error"):
                print(f"      error: {r['error']}")

        # ---- 产物下载
        header("产物下载")
        for res in results:
            for j, url in enumerate(res.get("urls") or [], 1):
                try:
                    path = download(res["case"], j, url)
                    print(f"  ✅ {res['case']}#{j} → {path}")
                    res.setdefault("files", []).append(path)
                except Exception as e:  # noqa: BLE001
                    print(f"  ❌ {res['case']}#{j} 下载失败：{type(e).__name__}: {e}")

        # ---- 汇总
        ok = sum(1 for r in results if r.get("status") == "succeeded")
        total_images = sum(r.get("images", 0) for r in results)
        print(f"\n{ok}/{len(results)} 用例成功，共 {total_images} 张产物")
        RESULT_JSON.parent.mkdir(parents=True, exist_ok=True)
        RESULT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"明细：{RESULT_JSON}")
        return 0 if ok == len(results) else 1
    finally:
        from app.observability import OBS  # noqa: PLC0415

        OBS.flush()
        svc.close()


if __name__ == "__main__":
    raise SystemExit(main())
