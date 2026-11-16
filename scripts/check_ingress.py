#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署后入口暴露面检查。

## 为什么是脚本而不是清单

规则是「**运维面在入口层 return 404，面板只走 SSH 隧道**」。写在文档里只是提醒，
写成可执行判据才会在每次部署后真的被检查 —— 2026-11-17 就是因为没有这一步，
面板被直接暴露在了公网。

## 判据

- **必须 404**：`/panel*`、`/stats`、`/docs`、`/redoc`、`/openapi.json`
  （自动文档同样属于运维面：它把完整参数面交给任何扫描器）；
- **必须 200**：`/healthz`、`/readyz`、`/async/v1/models`；
- 任一不符 ⇒ 退出码 1。

用法：

```bash
python scripts/check_ingress.py https://hailuo.1task.cn
```
"""
from __future__ import annotations

import sys

import httpx

#: 运维面：公网必须不可达（入口层 return 404）
DENY = (
    "/panel",
    "/panel/api/accounts",
    "/panel/app.js",
    "/stats",
    "/docs",
    "/redoc",
    "/openapi.json",
)

#: 业务与健康面：必须可用
ALLOW = (
    "/healthz",
    "/readyz",
    "/async/v1/models",
)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = (args[0] if args else "https://hailuo.1task.cn").rstrip("/")
    bad: list[str] = []

    def probe(c: httpx.Client, path: str) -> int | None:
        """返回状态码；连不上/超时返回 None（不能被当成"通过了"）。"""
        try:
            return c.get(base + path).status_code
        except httpx.HTTPError as e:
            print(f"   ! {path} 请求失败：{type(e).__name__}")
            return None

    with httpx.Client(timeout=15, follow_redirects=False) as c:
        for path in DENY:
            code = probe(c, path)
            ok = code == 404
            print(f"{'✅' if ok else '❌'} {path:<22} {code}  （期望 404）")
            if not ok:
                bad.append(f"{path} 暴露（HTTP {code}）")
        for path in ALLOW:
            code = probe(c, path)
            ok = code == 200
            print(f"{'✅' if ok else '❌'} {path:<22} {code}  （期望 200）")
            if not ok:
                bad.append(f"{path} 不可用（HTTP {code}）")

    if bad:
        print("\n入口策略被破坏：" + "；".join(bad))
        return 1
    print("\n入口策略符合：运维面不可达，业务面可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
