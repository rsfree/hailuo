#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gunicorn 配置。

## 🔴 末尾那对括号不能省

目标是**工厂**而不是模块级对象：

```bash
gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

写成 `app.main:app` 会得到 `Failed to find attribute 'app' in 'app.main'` /
`App failed to load.` —— 而**单测全绿也照样炸**，因为它们都直接调 `create_app()`。
`tests/test_wiring.py::test_dockerfile_cmd_target_resolves` 钉的就是这条。

## 默认单 worker 是**架构约束**，不是保守参数

节奏闸门（最小间隔 / 每分钟上限 / 风控冷却）是**进程内**状态，
副本数 N 等于把限速整体乘 N —— 恰好踩在上游风控最敏感的维度上。
协调器虽然靠数据库租约选主（多副本安全），但非持锁进程只会空转。
⇒ **提吞吐的正确顺序**：先把 `HL_CONCURRENCY` 提到实测上限，再考虑多副本。
"""
from __future__ import annotations

import os

#: 默认 1。见模块 docstring —— 改它之前先读 README 的"扩容前必读"。
workers = int(os.environ.get("WORKERS", "1"))
worker_class = "uvicorn.workers.UvicornWorker"

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8300')}"

#: 超时必须**大于**一次上游建任务的最坏耗时（含输入图下载 + 上传三段式），
#: 否则请求会被 gunicorn 掐掉，而任务其实已经建到上游了（钱花了、调用方没拿到 id）。
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

#: TLS：两个都配了才启用（与 `Settings.validate()` 的"成对"判据同源）。
_cert = os.environ.get("TLS_CERTFILE", "").strip()
_key = os.environ.get("TLS_KEYFILE", "").strip()
if _cert and _key:
    certfile = _cert
    keyfile = _key

#: 受理请求很轻（只落库），日志交给 loguru（应用内） ⇒ 不重复出 access log。
accesslog = None
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info").lower()

#: 进程名带上 worker 数，`ps` 一眼能看出是不是有人偷偷放宽了并发。
proc_name = f"hailuo-service[w{workers}]"

#: 预载能让多 worker 共享同一份只读内存；但本服务在 import 期**不连上游**，
#: 预载收益有限，而它会掩盖"某个 worker 启动失败"这件事 ⇒ 关闭。
preload_app = False

max_requests = 0        # 长驻进程；任务状态在库里，不需要靠重启换血
worker_tmp_dir = None
