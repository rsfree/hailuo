# hailuo-service

hailuo（`hailuoai.video`）的**异步**出口，两条链路同进程、互不影响：

**① 图片**（`POST` 受理 → `GET` 轮询两段式，参考 `../jimeng` 的骨架落地）

```
POST   /async/v1/images/generations        → 202 {"task_id": "hailuo_…"}   只回一个 id
GET    /async/v1/images/generations/{id}   → 202 排队态 / 200 {data,created,usage}
GET    /async/v1/images/generations        → 本 Key 的任务列表
DELETE /async/v1/images/generations/{id}   → 删除**已终态**的任务
GET    /async/v1/models                    → 图片能力清单 + 上游模型注册表
```

**② 视频**（**火山方舟 Seedance 原生协议**，路径逐字等于原生）

```
POST   /api/v3/contents/generations/tasks        → {"id": "cgt-…"}   只回一个 id
GET    /api/v3/contents/generations/tasks/{id}   → 原生任务对象（六态），**免鉴权**
GET    /api/v3/contents/generations/tasks        → {items,total,page_num,page_size}
DELETE /api/v3/contents/generations/tasks/{id}   → 删除**已终态**的任务
GET    /v1/models                                → OpenAI 形态清单（图片 + 视频），**免鉴权**
```

契约全文见 **[`docs/INTERFACE.md`](docs/INTERFACE.md)**（图片，冻结）与
**[`docs/SEEDANCE.md`](docs/SEEDANCE.md)**（视频，冻结）；
上游契约与逆向导出的结论见 **[`docs/UPSTREAM.md`](docs/UPSTREAM.md)**。

**图片重点能力是图生图（`hailuo-i2i`）** —— 见 §3。
**视频的核心是"输入形态内部自判"**：`content[]` 里给不给图、给首帧还是首尾帧，
决定落到 Hailuo 2.x 的三个模型槽位之一 —— 调用方不需要记住 26 个 modelID，
但**每一项被改动的参数都会出现在 `degradations` 里**。

---

## 0. 我要做什么 → 看哪个文件

| 我想… | 看这里 |
|---|---|
| 接图片链路 | `docs/INTERFACE.md` |
| 接视频链路（Seedance 协议） | `docs/SEEDANCE.md` |
| **改 `yy` 签名** | `app/upstream/hailuo/sign.py`（**5 条抓包向量自证**） |
| 改"图片能用什么 / 默认模型" | `app/models.py`（唯一的能力注册表） |
| 给客户端加模型别名（请求名 → 上游模型） | 面板「模型 → 模型别名」；落 `meta.model_aliases`，运行期优先于 `MODEL_ALIASES` |
| 改图片请求翻译（`size`/`n`/图片张数） | `app/service.py::build_plan`（**纯函数，重点覆盖**） |
| 改"视频能用什么 / 首尾帧怎么落" | `app/upstream/hailuo/video_models.py`（族 + 三个槽位） |
| 改视频请求翻译（`content[]` → 计划） | `app/video_service.py::build_video_plan`（**纯函数**） |
| 改视频响应形状 | `app/video_service.py::video_view` —— **唯一出口** |
| 改视频路由/鉴权 | `app/seedance_api.py` |
| 改上游请求构造 / 错误码分类 | `app/upstream/hailuo/client.py` |
| 改输入图上传 | `app/upstream/hailuo/upload.py` |
| 改输入图下载/归一化 | `app/media.py` |
| 改"什么时候轮到谁跑" | `app/coordinator.py` + `app/gate.py` |
| 改任务存取 | `app/store.py`（图片/视频**各一张表**，同一个实现） |
| 改图片响应形状 | `app/service.py::view` —— **唯一出口** |
| 改埋点 | `app/observability.py` —— **唯一收拢点** |
| 核对上游视频模型清单/价格（**零计费**） | `scripts/probe_video.py` |
| 管号池 / 改面板 | `app/panel.py` + `app/panel/`（静态页） |
| 改账号加密或选号 | `app/accounts.py`（密文/权重/冷却都在这里） |
| 改入口鉴权（透传 vs 池化） | `app/poolauth.py` |

---

## 1. 跑起来

```bash
cp .env.example .env      # 填 HAILUO_TOKEN
docker compose up -d --build
curl -s localhost:8300/healthz          # {"status":"ok"}
```

⚠️ **数据库刻意不发布宿主端口**（`db` 只有容器内网的 `5432/tcp`）：
应用走 compose 内网 `db:5432`，宿主端口毫无必要，而发布它只会制造冲突源
（本机 5432 常年被别的项目占着 ⇒ `compose up` 报 "port is already allocated"，
看着像本服务的问题，其实不是）。要在宿主上做管理：

```bash
docker compose exec db psql -U hailuo -d hailuo
```

本机直接跑（SQLite 即可，无需 PostgreSQL）：

```bash
export TASK_DB='sqlite+pysqlite:///./hailuo.db'
# 鉴权是**透传**：调用方请求里自带 hailuo JWT（见 §2.6），服务端无需任何账号凭据。
# HAILUO_TOKEN 只在跑 scripts/ 下的运维脚本时才需要（脚本登记它自己的凭据）。
export HAILUO_TOKEN='<浏览器 localStorage 里的 JWT>'   # 可选：仅脚本用
gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

🔴 末尾那对**括号不能省**：目标是**工厂**而不是模块级 `app` 对象。
写成 `app.main:app` 会得到 `Failed to find attribute 'app' in 'app.main'` /
`App failed to load.` —— **单测全绿也照样炸**，因为它们都直接调 `create_app()`。
`tests/test_wiring.py::test_dockerfile_cmd_target_resolves` 钉的就是这条。

⚠️ 直接用 gunicorn 跑时**记得设 `COORDINATOR_ENABLED=0`**，
否则它会真的去建任务（**计费**动作）。

### 凭据只要一个

hailuo **唯一的硬前提**是 `token` 头里的 JWT（浏览器 localStorage）。
没有 cookie、没有 aksk、没有 OAuth。
取法：登录 hailuoai.video → DevTools → Application → Local Storage → 复制 JWT。

⚠️ 它会**过期**（抓包样本 `exp` 距抓取约 26 天），过期后上游回 HTTP 401。

### 直接跑测试

```bash
python -m pytest -q        # 215 用例，零真实上游调用，约 4 秒
```

---

## 2. 这套设计的七条主线

### 2.1 受理不碰上游，协调器才是唯一执行者

`POST` **只落库就返回**（请求内零上游往返）。真正建任务由后台协调器做，
因为它要受节奏闸门约束、要能重试、要防止重复提交 —— 而**建任务是计费动作**。

hailuo 没有回调，只能自己轮询。⇒ 协调器**不是可选增强，是链路的一环**：
没有它，任务永远停在 `queued`（连建任务都不会发生），超时看门狗也永不触发。

### 2.2 🔴 一轮 tick 只打**一次**上游查询

这是本项目与即梦最大的**架构差异**，也是"把并发提上去"的前提。

即梦要按 id 逐个/合并查询；而 hailuo 的
`POST /api/feed/creation/my/batch` **不带 id 参数** —— 它回的是"我最近的 N 条"。
⇒ **一轮 tick 的上游查询次数与在途任务数完全无关，恒为 1 次。**

否则提并发等于把上游请求量一起乘 N，而那正是风控最敏感的维度。
`tests/test_service.py::test_poll_uses_one_upstream_call_for_many_tasks`
断言 4 个在途任务只产生 1 次查询。

### 2.3 并发上限放在库里数，不放在信号量里

`HL_CONCURRENCY` 的判据是 `count(status='in_progress')`。

用 `Semaphore` 的话，**进程重启后信号量归零**，而库里那些任务其实还在上游跑
⇒ 重启后会超发。按库计数天然重启安全，也天然跨 worker。

### 2.4 降级必须可见

任何"请求了 A、实际做了 B"都进响应的 `degradations`：默认模型代入、`size` 换算、
比例吸附、张数吸附、输入图归一化、能力表读不到而退回冻结快照。

**静默降级 = 让人按 A 的预期为 B 付钱。** 这条在 `build_plan` 里是硬约束：
凡是会改变结果或花费的取舍，都必须在那里留痕。

### 2.5 图生图（重点能力）的四条适配规则

1. **`fileList` 顺序 = 请求顺序**（上游按顺序理解垫图语义）；
2. **张数上限按"所选模型自己声明的" `maxSupportImageCount` 校验**
   （`gpt-image-1.5` 只有 **3**，`nano_banana*` 是 14，`gpt-image-2*` 是 16）——
   这是**服务端声明值**，不是经验值。超了就 400，**绝不"收下 N 张只用第 1 张"**；
3. **`referenceMode` 默认不发** —— 抓包的 i2i 请求里 `imageParameter` **没有**这个键；
4. **`useOriginPrompt` 恒 `true`** = "不要改写我的 prompt"。
   反过来等于让上游偷偷重写调用方的提示词，那是**没被要求的降级**。

### 2.6 鉴权：hailuo JWT **透传**（唯一的入口鉴权方式）

`Authorization: Bearer <你的 hailuo 登录 token>` —— 该 token **同时就是上游凭证**：
建任务、轮询、OSS 上传全程都用它，**费用记在 token 所有者账上**。
服务端**不持有任何账号凭据**，因此没有"白名单 key"或"服务端账号"这一档
（2026-09-21 起，且刻意**不做向后兼容**）。

- 本地只校验**结构与 `exp`**（**不验签** —— 没有签名密钥）；
  伪造/过期 token 由**上游**兜住（401）—— 本地这层只为"早失败、少一次往返"；
- 通过后 token 被换成**不可逆指纹** `credential_id = HMAC-SHA256(secret, token)`，
  `secret` 首次启动随机生成并存任务库 `meta` 表 ⇒ **明文 token 永不落库**；
- 明文 token 仅驻留**进程内存池**（`CredentialPool`，TTL/容量双上限），
  每个凭据一套客户端与上传缓存（上传资产是账号级的）；
- 任务与该指纹绑定：换 token 读别人的任务 ⇒ **404，且不发上游请求**（本地拦死）。

**已知弱点（不粉饰）**

| # | 问题 | 影响 |
|---|---|---|
| 1 | **不验签**：服务无法验证 token 真伪 | 伪造 token 会在上游拿到 401（本服务会把它作为任务失败如实返回）。本地这层只挡"结构/过期"这类明显坏输入 |
| 2 | **明文凭据在内存**（不落库的必然代价） | 进程被 dump ⇒ 在池 token 可见；进程重启 ⇒ 池清空，在途任务以 `503 credential_unavailable` 明确失败（**绝不回落**到别的账号） |
| 3 | **没有按凭据的配额/限流** | 闸门是**全局**的；任何合法 token 都能持续消耗**它自己账号**的额度 |
| 4 | **多凭据 ⇒ 轮询请求数按凭据数增长** | `my/batch` 的语义是"我最近的 N 条" ⇒ 每个有在途任务的凭据一轮各 1 次查询（单凭据部署与本设计早期完全一致：恒 1 次） |
| 5 | 无 IP 白名单 / 无 mTLS / 无审计日志（只有一行请求摘要） | — |

⇒ 生产部署建议：在网关层再叠一层鉴权与限流；把 `CredentialPool` 的容量当**内存预算**看。

### 2.7 不制造假能力

- **视频链路不注册**：上游模型信息端点一并返回 24 个 `videoModels`，
  但视频链路未取证 ⇒ 不出现在 `/async/v1/models`（见 `models.DELIBERATE_ABSENCES`）；
- 入口凭据不合法 ⇒ **401**（调用方的问题，说明为什么：结构/过期）；
- 任务凭据不在内存池（重启/过期）⇒ **503 `credential_unavailable`** 并**明确**
  告诉调用方"同 token 重提"——不是 401（那次请求当时合法），也不静默换账号；
- **查不到单价就不给 `forecast_credits`** —— 编一个数字等于说谎。

---

## 3. 图生图怎么调

```bash
curl -s -X POST localhost:8300/async/v1/images/generations \
  -H 'Authorization: Bearer <你的 hailuo token>' -H 'Content-Type: application/json' \
  -d '{"model":"hailuo-i2i","prompt":"把背景换成雪山，保留人物光影",
       "image":["https://…/ref.png"],"size":"4096x4096"}'
# → {"task_id":"hailuo_…"}

curl -s localhost:8300/async/v1/images/generations/hailuo_… \
  -H 'Authorization: Bearer <你的 hailuo token>'
# → 非终态：202 {"task_id":"…","status":"in_progress"}
# → 成功  ：200 {"data":[{"url":"https://cdn.hailuoai.video/…png"}],
#                "created":1789923012,"usage":{"images":1,"forecast_credits":8}}
```

要点：

- **`image` 必须是数组**，即使一张也要写 `["…"]`（传字符串会被明确拒绝并给出正确写法）；
- **张数上限取决于模型**：`gpt-image-1.5` 只能 3 张，`nano_banana*` 14 张，`gpt-image-2*` 16 张；
- 不写 `model` ⇒ 默认 `nano_banana21_flash`（**唯一有端到端抓包证据**的模型），并在 `degradations` 写明；
- 结果 URL 是**去水印**那条（`downloadURL.withoutWatermarkURL`）；
- 产物是上游直链，**不做转存**；有效期**未取证**。

---

## 3.9 视频怎么调（Seedance 协议）

```bash
# 文生视频（content 里只有 text ⇒ 内部落到 23204）
curl -s -X POST localhost:8300/api/v3/contents/generations/tasks \
  -H 'Authorization: Bearer <你的 hailuo token>' -H 'Content-Type: application/json' \
  -d '{"model":"hailuo-video","content":[{"type":"text","text":"镜头缓慢推近"}],
       "duration":6,"resolution":"1080p","ratio":"16:9"}'
# → {"id":"cgt-20260923004105-a1b2c3d4e5"}

# 首尾帧（两个 role ⇒ 内部落到 23210）
curl -s -X POST localhost:8300/api/v3/contents/generations/tasks \
  -H 'Authorization: Bearer <你的 hailuo token>' -H 'Content-Type: application/json' \
  -d '{"model":"hailuo-video","content":[
        {"type":"text","text":"人物转头微笑"},
        {"type":"image_url","image_url":{"url":"https://…/first.png"},"role":"first_frame"},
        {"type":"image_url","image_url":{"url":"https://…/last.png"},"role":"last_frame"}],
       "duration":6}'

# 查询（**免鉴权**，id 本身就是凭据）
curl -s localhost:8300/api/v3/contents/generations/tasks/cgt-20260923004105-a1b2c3d4e5
# → {"id":"cgt-…","status":"succeeded","content":{"video_url":"https://…/out.mp4"},
#    "usage":{"completion_tokens":0,"total_tokens":0,"forecast_credits":25},
#    "duration":6,"resolution":"1080p","ratio":"16:9","framespersecond":24,…}

# OpenAI 形态的模型清单（免鉴权）
curl -s localhost:8300/v1/models | jq '.data[].id'
```

要点：

- **形态由输入决定**：只有 text → 文生、有首帧 → 图生、首帧+尾帧 → 首尾帧。
  三者分别落到 `23204` / `23218` / `23210`（默认族 `hailuo-video`，**逐条对齐生产参考实现**）；
- **先看 `degradations`**：档位/时长被吸附、`ratio` 没转发给人、`watermark` 被忽略……
  全在那里逐条写明。**静默降级在这个项目里是不允许的** ——
  视频一次最高 180 积分，静默换档是最贵的一类 bug；
- **直接传上游 modelID**（如 `"model":"23218"`）也支持，零映射零说明；
  但它接不住你的输入时会 **400 并指出该用哪个**，不会悄悄换模型；
- `duration` 不在档位 ⇒ **向下吸附**（向上等于替人加钱）；`resolution` 吸附到最接近档；
- 🔴 **带框架图时档位有门禁**：上游按档位声明 `supportFrame`，只许落在声明支持的档上
  （`23210` 的 512 档**没**声明 —— 实测拿它跑首尾帧会得到 `code 2400001` 且白花积分）；
- 视频**单独一个看门狗** `VIDEO_TASK_TIMEOUT=3600`（视频是分钟级的，
  图片的 900s 会把还在跑的任务判死，而它仍在计费）；
- 视频轮询**主路径是 `my/batch`（`feedTypes=[0]`）**，产物在
  `metaInfo.videoMetaInfo.mediaInfo`；**`v4` 的 `batchType=0` 对视频恒回空**
  （实测 4 种 `type` 组合全空）⇒ 它只能做兜底，且必须用 **`data.id`（记录 id）** 当 batchID。
  ⚠️ 这条是**实测纠正**过来的：最初照参考实现只走 v4，结果上游 2 分钟出片、
  我们轮询 15 分钟没看见、最后判 `expired` —— **钱花了、片出来了、没拿到**。

**hailuo 系列实测跑通（2026-09-23，13 个模型里 11 个 ✅）**：

| 族 | modelID | 已验证形态 | 结果 |
|---|---|---|---|
| Hailuo 2.3 | `23204` / `23217` | 文生 / 仅首帧 | ✅ 145s、~2 min |
| Hailuo 2.3-Fast | `23218` | 仅首帧 | ✅ 85s（15 积分，最省） |
| Hailuo 2.0 | `23200` / `23210` | 文生 / **首尾帧** | ✅、1364×768 |
| Hailuo 1.0 / -Director / -Live | `23000` `23001` `23010` `23102` `23011` | 文生 / 仅首帧 | ✅ 5/5 全过 |
| MiniMax H3 / H3 Max | `hailuo3.0*` `hailuo_h3_max_*` | — | 🔴 `2200005 贝壳不足`（**免费账户不可用**，非余额问题） |

产物在 `e2e-output/video/`（11 段）；`usage.forecast_credits` 与计价表逐条对上。
未实跑：`veo3.1` / `sora2` / seedance2.0*（见 §6）。

---

## 3.95 管理面板与号池

面板是**号池的操作面**，不是装饰：账号的增删、停用、巡检、概览都在这里。

```bash
# 两个环境变量决定它存不存在
export ADMIN_TOKEN='<面板口令>'                      # 空 ⇒ /panel 404
export ACCOUNT_ENC_KEY="$(python -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
export SERVICE_KEYS='sk-…'                          # 可选：池化出号（留空=只透传）
curl -s localhost:8300/panel                        # 浏览器打开即可
```

**面板能力**（全部经 `ADMIN_TOKEN` 会话校验，Cookie 签名 TTL 12h）：

| 动作 | 端点 |
|---|---|
| 登录 / 登出 | `POST /panel/api/login` · `POST /panel/api/logout` |
| 列表 / 新增 / 删除 | `GET/POST /panel/api/accounts` · `DELETE /panel/api/accounts/{id}` |
| 停用 / 启用 | `POST /panel/api/accounts/{id}/status` |
| 批量导入 | `POST /panel/api/accounts/import`（每行 `备注<Tab>token` 或裸 token） |
| 巡检（单个 / 全部） | `POST /panel/api/accounts/{id}/check` · `.../check_all` |
| 概览 | `GET /panel/api/overview` |
| 模型别名（查 / 增改 / 删） | `GET/POST /panel/api/aliases` · `DELETE /panel/api/aliases/{alias}` |

**模型别名**（`客户端请求名 → 上游 modelID`）：面板「模型 → 模型别名」配置，
落任务库 `meta.model_aliases`（JSON），**改完立即生效**（模块级 copy-on-write，单 worker）。
优先级 **运行期 > 内置 `MODEL_ALIASES` > 原样透传**；删除运行期条目即自动回落内置值。
两条硬校验：目标是能力表里真实存在的 modelID；请求名不能是能力名/能力别名
（那会先于模型解析决定图生图/文生图路由，配了也不命中）。

**入口鉴权两条路，判据是 token 形态**（细节见 `app/poolauth.py`）：

1. **透传**：三段式 JWT ⇒ 该 token 就是上游凭证，费用记在调用方账上（旧行为，逐字不变）；
2. **池化**：逐字命中 `SERVICE_KEYS` ⇒ 服务从号池挑号，费用记在号主账上。

失败语义刻意分开：Key 不认识 ⇒ **401 `invalid_api_key`**；号池没有可用号 ⇒
**503 `credential_unavailable`**。**绝不"取号失败就回落透传"**——那会把一次本该 401 的请求
变成一次记在服务账号上的计费动作。

**加密与密钥纪律**（这条改变了旧铁律，写清楚）：

- 账号 token 以 **AES-256-GCM** 信封加密落库（`v1.<base64url(nonce|ct)>`），
  主密钥来自 `ACCOUNT_ENC_KEY`、**不入库**；库里另有 `HMAC-SHA256(secret, token)` 指纹，
  任务归属仍用指纹，**不解密也能查归属**；
- 面板与所有 API 响应**永不回显明文**（列表里连 `token_enc` 字段都没有）；
- 密钥换了 ⇒ 旧密文解不开，接口会明确报"密文无法解密"，**不会静默换号**。

**巡检是零成本的**：走 `my/processing`（只回答"有没有在跑的任务"），不建任务、不计费。
把巡检做成计费动作，等于每次点按钮都在烧钱。

**失败会回灌号池**（`Service.note_account_failure`）：上游 401 ⇒ 账号判 `dead`；
余额/贝壳不足 ⇒ 冷却 6h；429/风控 ⇒ 按 `HL_COOLDOWN` 降温。只影响**下次选号**，
不重试、不改写调用方看到的错误。

**选号判据**：`active` + 未过期 + 不在冷却，按 **权重降序 → 最久未用 → 最早到期** 取第一个。
最早到期者先用，是为了不让快到期的余额烂在库里。

**服务 Key 的命名规则**（用户口径，2026-11-17）：

**`sk-` + 二级域名**，直接拼、不加前后缀：`hailuo.1task.cn` ⇒ `sk-hailuo`。
一个服务一个 key，不用全局万能 key；多把时**同名变量、逗号分隔**。
写进该服务的 `.env`，**不入库、不进 README**。

```bash
HAILUO_API_KEYS=sk-hailuo          # 标签自动推导为 hailuo
# 多把（同名变量、逗号分隔）：
# HAILUO_API_KEYS=sk-hailuo,sk-hailuo-2
```

**需要配额时才用扩展写法**（Key 与标签里都不许出现冒号）：

```
HAILUO_API_KEYS=tenant-a:sk-aaa:200,tenant-b:sk-bbb
#                └标签┘ └Key┘ └日配额┘  └标签:Key┘
```

- 配额单位是**每日受理次数**，不是积分：`forecast_credits` 对部分模型可能为空，
  用积分配额就必须在缺价时编一个数来扣 —— 与"查不到单价就不给数字"的纪律冲突。
  请求数永远可计算，所以选它；
- 超限 ⇒ **429 `quota_exceeded`**（不是限流 429：那是"太快"，这是"今天没了"），
  **不落库、不计费**；用量按 `(owner_id, 日期)` 存在 `key_usage` 表，跨天自动归零；
- 配额在**参数校验通过之后**才扣：写错参数的请求不吃额度；
- 并发下（多 worker）检查与自增在同一事务，但**没有行锁** ⇒ 极小概率多放过一两条。
  SQLite 部署无此问题；Postgres 严格上限需改 `SELECT … FOR UPDATE`，这里不假装是硬的。

**多账号轮换（选号规则）**：

- 判据：`active` + 未过期（留 60s 余量）+ 不在冷却，按 **权重降序 → 最久未用 → 最早到期**
  排序取第一个。等权重下就是 LRU 轮换，权重高的号被选中得更频繁；
- **满载账号先跳过**：按库里 `in_progress` 计数，达到 `HL_CONCURRENCY` 的号不参与本轮选号
  —— 否则新请求会压在一个已经在跑的号上白排队。全忙时**退回**全体可用号（宁可排队，不判 503）；
- **建任务前失败自动换号**：仅当失败是 `pre_create`（上游一定没建任务、未计费）且处于池化模式，
  换号只改 `credential_id`，**不改 `owner_id`**（归属不变）。透传模式**绝不换号**——
  那会把费用记到别人账上。

**多服务 Key 的任务隔离**（2026-11-17 起）：

- 任务表新增 `owner_id`：**谁看得见** 与 **用哪个号跑**（`credential_id`）分离。
  池化模式下 owner = 服务 Key 指纹 ⇒ 两个 Key 共用同一账号也互不可见（列表为空、
  单条/删除 404）；透传模式下两者同值，行为与旧版逐字一致；
- 老库由启动期幂等迁移补列并回填（`ALTER TABLE … ADD COLUMN` + `UPDATE … = credential_id`），
  升级**不会**让历史任务"消失"。

**面板 TLS**：

- `TLS_CERTFILE` / `TLS_KEYFILE` **必须成对**，缺一个启动即失败（不做半个 TLS）；
  gunicorn 与 `uvicorn.run` 都会带上证书；
- HTTPS（或 `X-Forwarded-Proto: https`）下会话 Cookie 自动加 `Secure`；
  `ADMIN_TOKEN` 已配但既无证书又走明文 ⇒ **启动告警**，不静默。

**上游对账（2026-11-17 起）**：

建任务请求发出、上游已受理并计费，但响应丢在网络上（或进程恰在此刻被杀）
⇒ 本地只有 `attempts`、没有 `upstream_batch_id`。这是**唯一会造成资损**的路径：
调用方看到"失败"，钱和产物都白扔。

协调器每 `RECONCILE_INTERVAL`（默认 300s）跑一次，判据四条**同时**成立：
批次未被任何本地任务占用 / `model_id` 相同 / `desc` 与 prompt 逐字相同 /
批次创建时间与本地提交时间偏差 ≤ `RECONCILE_WINDOW`（默认 180s）。
**命中唯一才认领**，多个记 `ambiguous` 放弃 —— 错认的代价（把别人的图交给你）
远大于晚一点交付。面板 `POST /panel/api/reconcile` 可手动触发。

**凭据留存（2026-11-17 起）**：

- **池化账号**：token 以 AES-256-GCM 密文存 `accounts`，任务只记指纹
  ⇒ 进程重启后按指纹解密恢复，在途任务不断；
- **透传调用方**：默认 `STORE_CALLER_TOKENS=1`，把调用方 JWT 加密存 `caller_tokens`
  （按指纹去重，TTL = JWT 的 `exp`，过期即删）。**这是一次安全取舍**：
  库里有了调用方 token 的密文，主密钥泄露的影响面从"你自己的号"扩大到"所有调用方"。
  要回到"永不落库"请设 `STORE_CALLER_TOKENS=0`，代价是重启后透传任务明确失败。
- 两种情况都**只存密文**，任何接口/面板都不回显明文。

**产物下载**：脚本对 CDN 建连抖动做 4 次退避重试，并校验状态码 + 魔术字节
（CDN 出错会回 HTML，存成 .png 报"成功"是最坏的一种谎报）。

**已知边界（不粉饰）**：

- 任务隔离到**服务 Key** 粒度（owner_id），但没有"团队/角色"概念，也没有按 Key 的配额；
- 面板可自带 TLS（成对证书），但仍只有单管理员口令，没有多用户/审计日志
  （每个动作只留一行 loguru 摘要）；
- 生产仍建议网关层再叠 IP 白名单与限流。

## 3.99 入口暴露面（部署后必查）

**规则：`/panel` 公网可达，其余运维面在入口层 `return 404`。**

公网必须不可达：`/stats`、`/docs`、`/redoc`、`/openapi.json`
（自动文档同属运维面 —— 它把完整参数面交给任何扫描器）。
公网必须可用：`/healthz`、`/readyz`、`/async/v1/models`、`/panel`。

> **口径变更 2026-11-19（用户决策，参考 workbuddy2api）**：面板由「只走 SSH 隧道」
> 改为**公网开放**，且 `ADMIN_TOKEN` 与服务 Key 同值（`sk-hailuo`）。
> 代价必须写清楚：**持有服务 Key 的调用方 = 管理员**（能加号/停号/触发巡检/看用量）；
> 登录接口没有失败锁定，爆破防护只剩"Key 本身的熵"与网关限流。
> 因此服务 Key 的发放范围 = 管理面信任半径，不得再当"给调用方随便发的 token"用。

```bash
python scripts/check_ingress.py https://hailuo.1task.cn   # 任一不符 ⇒ 退出码 1
```

nginx 侧写法（`/www/server/panel/vhost/nginx/hailuo.1task.cn.conf`，`location /` 之前）：

```nginx
location = /stats        { return 404; }
location = /docs         { return 404; }
location = /redoc        { return 404; }
location = /openapi.json { return 404; }
```

面板入口：**`https://<域名>/panel`**，登录口令即服务 Key（`sk-` + 二级域名）。

隧道方式仍保留，用于入口被误改或需要绕开公网时的救援：

```bash
ssh -J jump -N -L 18300:127.0.0.1:8300 root@<host>
# 然后浏览器打开 http://127.0.0.1:18300/panel
```

## 4. 扩容前必读

**默认 `WORKERS=1` 是架构约束，不是保守参数：**

- 节奏闸门（最小间隔 / 每分钟上限 / 风控冷却）是**进程内**状态，
  副本数 N 等于把限速整体乘 N —— 恰好踩在上游风控最敏感的维度；
- 协调器靠数据库租约选主，多 worker 虽安全但非持锁进程只会空转。

**`HL_CONCURRENCY` 的语义是「每个号」**（`slots = HL_CONCURRENCY - active[credential]`），
不是全局：号池里 N 个账号 ⇒ 全局在途 ≈ N × 该值。当前部署口径是**每号 4**
（2026-11-17 用户指定；启动会保留一条"上游并发上限未实测"的告警，这是有意留的）。

**提吞吐的正确顺序**：先确认单号并发（本项目**未实测上游并发上限**，4 是策略选择），
再考虑多副本。放宽并发会成倍放大额度消耗速率与风控暴露面，是**策略选择**不是技术限制。

---

## 5. 测试

```bash
python -m pytest -q
```

- **零真实上游调用**：所有用例注入假上游（`httpx.MockTransport` 覆盖**全部**端点），
  一个字节都不发出去。建任务是计费动作，这条红线由夹具保证，
  而不是靠"测试里记得别调真接口"。
- **缺依赖就响亮失败，不静默跳过** —— 跳过会让人把"没跑"当成"跑过了"。
- **测试绝不碰真实 DNS**：`_is_private_host` 会对域名解析，
  在受限环境里那是一次真实网络调用（实测把整进程跑挂）——
  `tests/test_media.py` 有个 autouse 夹具把解析换成本地确定值。

| 文件 | 覆盖 |
|---|---|
| `test_sign.py` | **5 条抓包向量** + `encodeURIComponent` 语义 + 键序/紧凑格式 |
| `test_service.py` | **翻译层**（`build_plan`）+ 提交/轮询/响应形状/归属/删除 |
| `test_api.py` | HTTP 契约（`docs/INTERFACE.md` 的可执行版本） |
| `test_upstream.py` | 客户端信封与错误映射 + 上传 + 能力表解析 |
| `test_media.py` | 魔数嗅探 / SSRF 防线 / 归一化 |
| `test_wiring.py` | 可观测性纪律 / **配置旋钮** / 闸门 / 存储 / 协调器 / 注册表 |

### 端到端探针：零成本优先，花钱要显式开闸

```bash
# 全部零成本阶段（默认）
python scripts/e2e.py

# 加上真实上传（免费，但会在账号里留一个素材）
python scripts/e2e.py --phases sign,capabilities,accept,dry,upload --allow-upload

# 🔴 真实出图（**扣额度**；不加 --allow-real-submit 只打印"将要发生什么"）
python scripts/e2e.py --phases generate --allow-real-submit
```

| 阶段 | 花额度 | 做什么 |
|---|---|---|
| `sign` | ❌ | 5 条抓包向量**离线**复算（**不出网**） |
| `capabilities` | ❌ | 读两个公开配置端点，打印实读到的模型表 |
| `probe` | ❌ | 用 `my/processing` 做凭据自检（不建任务） |
| `accept` | ❌ | 受理并断言**只回一个 task_id**（协调器没启 ⇒ 零上游往返） |
| `dry` | ❌ | 走完整条翻译链：签名 + 查询串 + body 全构造，**不发请求** |
| `upload` | ❌ | 真打上游跑 OSS 三段式（免费；需 `--allow-upload`） |
| `generate` | 🔴 | 真建任务 + 轮询到终态（需 `--allow-real-submit`） |

`generate` 单独隔出来，是因为建任务是**计费动作**。

### 接线门禁（`tests/test_wiring.py` + `ruff.toml`）

门禁分两层，缺一层都会漏：

- **静态**：`ruff check`（`F` / `E9` / `BLE` / `S110` / `PLC0415` / `SLF001`），
  **刻意不收风格规则** —— 一次报上百条的门禁等于没有门禁；
- **动态**：`test_wiring.py` 里的用例**真的把链路跑一遍**
  （用 `httpx.MockTransport` 打上游，断言**确实产生了一条上报**）——
  "接线"这件事只有跑才证明得了。

两条配置门禁尤其值得留：**每个被读的配置项都必须存在**（否则 `AttributeError`）、
**每个配置项都必须有人读**（没人读的旋钮 = 假配置）。
后者在本次开发中**真的抓到了 3 个死旋钮**：`HL_MAX_WAIT`（与 `TASK_TIMEOUT` 重复）、
`MAX_INPUT_BYTES`（与 `MAX_DOWNLOAD_BYTES` 重复）、`POLL_GRACE`（读了却没消费）。
前两个已删除（一个概念只留一个旋钮），第三个已接线并有用例守着。

---

## 6. 诚实边界（未取证的事）

完整清单在 `docs/UPSTREAM.md` §8（图片）与 `docs/SEEDANCE.md` §6（视频）。

| 事项 | 状态 |
|---|---|
| **图片端到端真实出图** | 🔴 **本项目未做**（建任务即计费）。上游链路已按抓包与非计费只读调用逐层验证，但"建任务 → 出图"这一段**没有实跑过** |
| 图片产物 URL 有效期 | **未取证** ⇒ 原样透传、不做转存 |
| 上游并发/频控阈值 | **未界定** ⇒ 默认并发 1 是策略选择，不是实测上限 |
| **视频端到端真实出片** | ✅ **hailuo 系列 11/13 已实跑**（2026-09-23，见 §3.9 台账）。未实跑：`veo3.1` / `sora2` / seedance2.0* —— body 同构，但各自可能另有前置校验（512 档那次就是） |
| 视频的**账户档位门槛** | 🔴 实测：`hailuo3.0*` 与 `hailuo_h3_max_*` 对**免费账户**（`vipInfo.type=0`）回 `2200005 贝壳不足`；同一时刻 2.x/1.0 系**照常可用** ⇒ 是档位门槛不是余额。**无只读端点可预知贝壳是否够**（`/v1/api/user/info` 里没有余额字段） |
| 视频 `my/batch` 的产物字段路径 | ✅ **已取证**：`metaInfo.videoMetaInfo.mediaInfo.{url,downloadURL,width,height}`（同一次实测） |
| 视频 `v4` 的可用性 | 🔴 **实测：`batchType=0` 对视频恒回空**（4 种 `type` 组合全空）⇒ 只能按**记录 id** 兜底。若上游日后改了它，`verify_video_poll.py` 会立刻发现 |
| 视频多参考图 / 主体参考 / 视频延长 / 音频参考 | **未取证** ⇒ 不实现，传了就 **400**（不静默降级） |

---

## 7. 目录

```
app/
  main.py             FastAPI 装配：路由 / 错误信封 / lifespan / 埋点接线
  config.py           Settings（每个旋钮都必须有人读）
  errors.py           错误分类体系（对外错误信封）
  models.py           **图片**能力注册表 + 上游模型冻结快照（唯一真相）
  media.py            输入图嗅探 / 下载（含 SSRF 防线）/ 归一化
  service.py          **图片**编排：受理 / 翻译（build_plan）/ 提交 / 轮询 / 响应构造
  video_service.py    **视频**编排：Seedance content[] 翻译 / 提交 / v4 轮询 / 原生响应
  seedance_api.py     火山 Seedance 协议端点 + `/v1/models`
  coordinator.py      后台协调器（推进任务的唯一执行者；图片/视频各一个）
  gate.py             节奏闸门（间隔 / 每分钟上限 / 风控冷却）
  store.py            SQLAlchemy 任务存储（tasks / video_tasks 两表同一实现）+ 租约
  observability.py    logfire + loguru 单一收拢点
  accounts.py         **号池**：AES-GCM 密文 / 指纹 / 权重 / 冷却 / 选号
  poolauth.py         入口鉴权：JWT 透传 或 服务 Key 从池取号
  panel.py            管理面板 API（会话 / 账号 CRUD / 零成本巡检）
  panel/              面板静态页（index.html + app.js + style.css，无构建步骤）
  upstream/hailuo/    sign（yy 签名）/ client / upload（OSS STS）/ capabilities
                      + video_models（视频注册表与族路由）/ video_capabilities
tests/                见 §5
docs/                 INTERFACE.md（图片契约）/ SEEDANCE.md（视频契约）
                      / UPSTREAM.md（上游契约与逆向结论）
scripts/e2e.py        零成本优先的端到端探针（图片）
scripts/probe_video.py 上游视频模型清单/价格对账（**零计费**，不建任务）
```
