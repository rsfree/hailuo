# 视频出口：火山方舟 Seedance 协议（冻结）

> 本文件是**视频链路唯一的对外契约真相**。改动 = 破坏调用方，必须同步
> `tests/test_video.py` 与 `docs/INTERFACE.md` 的索引。
> 上游侧的模型清单、档位与单价见 `app/upstream/hailuo/video_models.py`；
> 图片链路见 `docs/INTERFACE.md`（两者同进程、**不同表**、互不影响）。

---

## 0. 端点

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| `POST` | `/api/v3/contents/generations/tasks` | **要** | 受理，**只回一个 `id`** |
| `GET` | `/api/v3/contents/generations/tasks/{id}` | **免** | 原生任务对象（六态） |
| `GET` | `/api/v3/contents/generations/tasks` | 默认**要** | `{items, total, page_num, page_size}` |
| `DELETE` | `/api/v3/contents/generations/tasks/{id}` | **要** | 删除**已终态**的任务 |
| `GET` | `/v1/models` | **免** | OpenAI 形态的模型清单（图片 + 视频） |

路径**逐字等于原生**：调用方只改 `Base URL` 与 `API Key`。

### 鉴权为什么与"GET 全免鉴"有出入

- **写**（`POST`/`DELETE`）：`Authorization: Bearer <hailuo 登录 JWT>` —— 透传，
  与图片链路同一姿势，服务端不持有账号凭据。
- **`GET` 单条**：**免鉴权**。`cgt-` id 的随机段是 10 位十六进制（约 10¹² 空间），
  **id 本身就是凭据**，与图片链路"id 即凭据"一致。
- **`GET` 列表**：**默认要鉴权**（`VIDEO_LIST_REQUIRE_AUTH=1`）。
  理由：列表会把**所有**任务的 id 交给任何未带凭据的人 —— 而 id 就是读接口的凭据，
  开放列表等于**把凭据派发出去**。要开放请显式设 `VIDEO_LIST_REQUIRE_AUTH=0`
  （带凭据时仍只列自己的任务）。

---

## 1. 受理

```http
POST /api/v3/contents/generations/tasks
Authorization: Bearer <hailuo 登录 token>
Content-Type: application/json

{
  "model": "hailuo-video",
  "content": [
    { "type": "text", "text": "镜头缓慢推近，人物转头微笑" },
    { "type": "image_url",
      "image_url": { "url": "https://…/first.png" },
      "role": "first_frame" },
    { "type": "image_url",
      "image_url": { "url": "https://…/last.png" },
      "role": "last_frame" }
  ],
  "duration": 6,
  "resolution": "1080p",
  "ratio": "16:9"
}
```

**响应 `200`**：

```json
{ "id": "cgt-20260923004105-a1b2c3d4e5" }
```

🔴 **只有 `id` 一个键** —— 与原生一致。不返回状态/时间戳等附加信息。

**请求内零上游往返**：建任务（计费动作）交给后台协调器，受节奏闸门约束；
框架图的下载与上传同样在后台 ⇒ 上游网络抖动体现为任务 `failed`（附原因），
而不是让受理请求跟着一起抖。

### `content[]` 的规则

| 元素 | 规则 |
|---|---|
| `{"type":"text","text":…}` | 提示词。多条按出现顺序**换行拼接**（原生只允许一条 ⇒ 拼接是本服务的口径，**进 `degradations`**） |
| `{"type":"image_url","image_url":{"url":…},"role":"first_frame"}` | 首帧 |
| `… "role":"last_frame"` | 尾帧（**必须配首帧**，见下） |
| `image_url` 省略 `role` | 按**首帧**理解（原生图生视频的默认语义：那张图就是起始画面） |
| 其它 `role`（如 `reference_image`） | **400** —— 多参考图语义在上游未取证，静默当首帧会改变"用户想要什么" |
| 同一角色出现两次 | **400** |
| `image_url.url` 支持 | 公网 URL 或 `data:image/*;base64,…`（**本地图唯一入口**，HTTP 接口不收裸文件） |

### 🔴 档位门禁：`supportFrame` 是**按档位**声明的

上游每个档位可以单独声明 `addition.supportFrame`。**实测**（2026-09-23）：
`23210` 的 512 档**没有**这个声明，而 768/1080 是 `true` ——
拿 512 去跑首尾帧，上游 6 秒后回 `code 2400001 生成内容出错了，请重新生成`，
**看不到任何"档位选错了"的线索**，还白花一次积分。

⇒ 本服务在带**任何框架图**时只允许落在**上游明确声明 `supportFrame: true`** 的档位上：

| 情形 | 行为 |
|---|---|
| 请求的档位已声明支持 | 直接用 |
| 请求的档位未声明、但模型有声明支持的档位 | **改到最近的声明档**，并进 `degradations` |
| 请求的档位**明确**声明 `supportFrame: false` 且无替代档 | **400**（不放行、也不悄悄换档） |
| 模型一档都没声明（如 `23218`） | 放行，但 `degradations` 写明"框架图能力未经声明校验" |

**默认别传 `resolution`** ⇒ 会落到上游默认档（`23210` 是 768，正是声明支持框架图的那一档）。

### 🔴 模型由**输入形态**内部决定（这是本服务的核心行为）

调用方不需要记住上游的 26 个 modelID。形态由 `content[]` 判定：

| 输入形态 | 槽位 | 默认族 `hailuo-video` 落到 |
|---|---|---|
| 只有 text | `t2v` | `23204`（Hailuo 2.3 文生） |
| 有首帧 | `first` | `23218`（Hailuo 2.3-Fast 图生，768p/6s **15 积分**，2.x 最便宜） |
| 首帧 + 尾帧 | `pair` | `23210`（Hailuo 2.0 图生，支持首尾帧） |

这三个槽位**逐条对齐**生产参考实现（`MeUtils/apis/hailuoai/openai_videos.create_task`）。

**其它可用能力名**（`model` 字段）：

| 能力名 | 别名 | 文生 | 首帧 | 首尾帧 |
|---|---|---|---|---|
| `hailuo-video` | `auto` / `video` / `hailuo-2.x` | `23204` | `23218` | `23210` |
| `hailuo-2.3` | `2.3` | `23204` | `23217` | `23217` |
| `hailuo-2.3-fast` | — | ✗（400） | `23218` | `23218` |
| `hailuo-2.0` | `2.0` | `23200` | `23210` | `23210` |
| `hailuo-3.0` | `minimax-h3` / `h3` | `hailuo3.0-t2v` | `hailuo3.0-i2v` | `hailuo3.0-i2v` |
| `hailuo-3.0-max` | `h3-max` | `hailuo_h3_max_t2v` | `hailuo_h3_max_i2v` | `hailuo_h3_max_i2v` |
| `veo3.1` | `veo` | `veo3.1-t2v` | `veo3.1-i2v` | `veo3.1-i2v` |
| `veo3.1-fast` | — | `veo3.1-t2v-fast` | `veo3.1-i2v-fast` | `veo3.1-i2v-fast` |
| `sora2` | `sora-2` | `sora2-t2v` | `sora2-i2v` | ✗（只允许 1 张图） |
| `hailuo-1.0` | `1.0` / `t2v-01` | `23000` | `23001` | ✗（只允许 1 张图） |
| `hailuo-1.0-director` | `director` | `23010` | `23102` | ✗ |
| `hailuo-1.0-live` | `live` | ✗（400） | `23011` | `23011` |
| `seedance-2.0` | `seedance` | `seedance2.0-t2v` | `seedance2.0-i2v` | `seedance2.0-i2v` |
| `seedance-2.0-fast` | — | `seedance2.0-fast-t2v` | `seedance2.0-fast-i2v` | 同左 |
| `seedance-2.0-mini` | — | `seedance2.0-mini-t2v` | `seedance2.0-mini-i2v` | 同左 |

**直接传上游 modelID**（如 `"model": "23218"`）也支持 —— **零映射零说明**，
就是那个模型本身。⚠️ 但它**接不住你的输入时会 400 并指出该用哪个**，
**绝不静默换模型**：视频单价差最高 6 倍（`23218` 15 积分 vs `veo3.1` 180 积分），
悄悄换一个等于让人按 A 的预期为 B 付钱。

### 其它请求字段

| 字段 | 行为 |
|---|---|
| `duration` | 整数秒。不在该模型档位 ⇒ **向下吸附**（向上等于替人加钱）并留痕；`-1` ⇒ 用上游默认档 |
| `frames` | 按 **24fps** 换算成 `duration`（换算这件事进 `degradations`）；与 `duration` 同时给时以 `duration` 为准 |
| `resolution` | `480p`/`720p`/`1080p`/`4k` → 上游裸数字（`480`/`720`/`1080`/`3840`）。不在档位 ⇒ **吸附到最接近的**并留痕 |
| `ratio` | `16:9`/`4:3`/`1:1`/`3:4`/`9:16`/`21:9`/`adaptive`。`adaptive` ⇒ 上游 `Auto`。**模型未声明比例档位时不转发**（2.x 系就是这样）并留痕 |
| `watermark` / `seed` / `camera_fixed` / `generate_audio` / `return_last_frame` / `callback_url` / `service_tier` / `execution_expires_after` / `priority` | **不报错**，进 `degradations`（上游没有这些概念，详见 `video_service.VIDEO_KNOWN_UNSUPPORTED`） |
| 其它未知字段 | **400** |

### `endFrameRequiredStartFrame`

上游为 `veo3.1-i2v`、`hailuo_h3_max_i2v`、`seedance2.0-i2v` 等声明了这个标记：
**给尾帧就必须同时给首帧**。只给 `last_frame` ⇒ **400**，不猜。

---

## 2. 查询

```http
GET /api/v3/contents/generations/tasks/cgt-20260923004105-a1b2c3d4e5
```

**不需要 Authorization。** 响应是原生任务对象：

```json
{
  "id": "cgt-20260923004105-a1b2c3d4e5",
  "model": "hailuo-video",
  "status": "succeeded",
  "content": { "video_url": "https://…/out.mp4" },
  "usage": { "completion_tokens": 0, "total_tokens": 0, "forecast_credits": 25 },
  "created_at": 1790099465,
  "updated_at": 1790099520,
  "seed": -1,
  "resolution": "1080p",
  "ratio": "16:9",
  "duration": 6,
  "framespersecond": 24,
  "service_tier": "default",
  "execution_expires_after": 604800
}
```

### 状态六态

| `status` | 何时 |
|---|---|
| `queued` | 已受理、未建到上游（或上游还没开始） |
| `running` | 上游生成中 |
| `succeeded` | `content.video_url` 可用 |
| `failed` | 上游判失败（`error.code` 见下） |
| `expired` | **本地看门狗**判定超时（`VIDEO_TASK_TIMEOUT`，默认 3600s）⚠️ 上游可能仍在跑并计费 |
| `cancelled` | **不会出现** —— hailuo 没有取消端点，本服务不伪造这个状态（`DELETE` 只输**已终态**的记录） |

### 🔴 轮询怎么走（**实测纠正过**，别再改回去）

| 路径 | 形态 | 结论 |
|---|---|---|
| **`POST /api/feed/creation/my/batch`**（`feedTypes=[0]`） | 产物在 `batchFeeds[].feeds[].metaInfo.videoMetaInfo.mediaInfo` | ✅ **主路径**。一次请求覆盖全部在途任务，与在途数无关 |
| `POST /v4/api/multimodal/video/processing`（`batchType=0`） | `batchVideos[].assets[]` | ❌ **对视频批次恒回空**（`type` 的 4 种组合实测全空）⇒ 不能当主路径 |
| 同上，但 `batchID` 用 **`data.id`（记录 id）** | 同上 | ✅ 可用的**兜底**（记录 id 已存进 `upstream_feed_id`） |

⚠️ 本服务最初照生产参考实现**只走 v4**，结果：上游 2 分钟就出片、我们轮询了 15 分钟
什么都没看到、最后被看门狗判 `expired` —— **钱花了、片出来了、没拿到**。
主路径与兜底现在由 `tests/test_video.py` 各自钉死一条用例。

`my/batch` 里“挤不进最近 N 条”的任务由 **v4 + 记录 id** 兜底 ⇒ 每轮至多 2 次上游查询。

### 几个刻意的取舍

- ⚠️ **失败也回 HTTP 200**：任务本身完成了，只是结果是失败；回 4xx 会误触发
  调用方的重试逻辑。判据看 `status` 与 `error`。
- 🟡 `usage` 里 `completion_tokens`/`total_tokens` 恒为 `0` —— 上游**不按 token 计费**，
  编一个数字就是伪造。真实成本信息在**加性扩展** `usage.forecast_credits`
  （来自上游计价表的**预估**积分，不是账单实扣值）。
- 🟡 降级留痕（`degradations`）**不进 HTTP 响应**（2026-10-10 接口字段收敛）：
  逐条说明"请求了 A、实际做了 B"，只进**服务日志**（受理时 `降级留痕：` 前缀，
  带 task_id）与任务库 `degradations_json`。排查以日志为准。
- 🟡 `frames` 换算、档位吸附、`ratio` 不转发这件事**都在降级留痕里** ——
  静默降级等于让人按 A 的预期为 B 付钱。

---

## 3. 列表与删除

```http
GET    /api/v3/contents/generations/tasks?page_num=1&page_size=20
DELETE /api/v3/contents/generations/tasks/{id}
```

列表返回 `{items, total, page_num, page_size}`，`items[]` 与单条查询**同形**。
`page_size` 上限 100。

`DELETE` 只接受**已终态**（`succeeded`/`failed`/`expired`）的任务；未终态 ⇒ **400**：

```
⚠️ hailuo 没有取消端点：本地删掉只会让"它还在上游跑并可能计费"变成看不见的事。
```

---

## 4. `GET /v1/models`（OpenAI 兼容）

```json
{
  "object": "list",
  "data": [
    { "id": "hailuo-video", "object": "model", "created": 0, "owned_by": "hailuo",
      "hailuo": { "kind": "capability", "slots": { "t2v": "23204", "first_frame": "23218",
                                                   "first_last_frame": "23210" } } },
    { "id": "23218", "object": "model", "created": 0, "owned_by": "hailuo",
      "hailuo": { "kind": "video_model", "durations": [6, 10], "resolutions": ["768", "1080"] } },
    { "id": "nano_banana21_flash", "object": "model", "created": 0, "owned_by": "hailuo",
      "hailuo": { "max_images": 14, "resolutions": ["1K", "2K", "4K"] } }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `id` / `object` / `created` / `owned_by` | OpenAI 字段集，所有客户端都吃 |
| `hailuo` | 本服务的扩展块（族/槽位/档位/单价/上限），OpenAI 客户端会忽略 |
| `hailuo.routable` | 🔴 **本服务能不能真的服务它**。`false` = 上游有、但 `content[]` 语义接不住（`s2v` / `extend`） |
| `hailuo.routable_note` | `routable: false` 时的**原因**（传了会 400，别拿它去试） |
| 覆盖范围 | **图片 + 视频**（这是"这个服务有哪些模型"的完整答案） |

🔴 **不可路由的模型照样列出**（带 `routable: false` + 原因）。把它们从清单里吞掉，
调用方会以为"这服务没这个模型"，而实际上传了会被 400 —— 两种失败都要能解释。
可路由的模型传 `model=<上游 modelID>` 会被 `route_exact` 接住；
不可路由的会被本地明确拒绝，**不会**变成一次失败计费。

图片链路另有 `/async/v1/models`（OpenAI 形态 + 上游图片模型注册表），两者不冲突。

---

## 5. 零计费的自检入口

```bash
# ① 与上游实读对账（清单/档位/单价/族路由）—— 不带 token、不建任务
<venv>/bin/python scripts/probe_video.py [--dump]

# ② 拿一个**已存在**的真实批次复核轮询收口（只发查询）—— 零计费
<venv>/bin/python scripts/verify_video_poll.py --batch <batchID> --record <recordID>
```

`verify_video_poll.py` 是把"轮询收口 + 响应成形"在**真数据**上重放一遍，
不需要再花钱建任务 —— 改过轮询后**务必先跑它**。

## 5.1 真实端到端实测（🔴 **会计费**）

```bash
<venv>/bin/python scripts/e2e_video.py --mode pair --resolution 768p --duration 6
```

**hailuo 系列实测台账（2026-09-23，真实计费 · 13 个模型里 11 个 ✅）**：

| 族 | 上游 modelID | 已验证形态 | 结果 |
|---|---|---|---|
| Hailuo 2.3 | `23204` | 文生 | ✅ 145s / 0.85 MB（25 积分） |
| Hailuo 2.3 | `23217` | 仅首帧 | ✅ ~2 min / 2.55 MB（25 积分） |
| Hailuo 2.3-Fast | `23218` | 仅首帧 | ✅ 85s / 0.93 MB（15 积分） |
| Hailuo 2.0 | `23200` | 文生 | ✅ 0.43 MB（25 积分） |
| Hailuo 2.0 | `23210` | **首帧+尾帧** | ✅ ~132s / 1364×768 / 824 KB（25 积分） |
| Hailuo 1.0 | `23000` | 文生 | ✅ 2.56 MB（**无价目行** ⇒ `forecast_credits` 缺失） |
| Hailuo 1.0 | `23001` | 仅首帧 | ✅ 1.39 MB |
| Hailuo 1.0-Director | `23010` | 文生 | ✅ 0.78 MB |
| Hailuo 1.0-Director | `23102` | 仅首帧 | ✅ 2.04 MB |
| Hailuo 1.0-Live | `23011` | 仅首帧 | ✅ 2.06 MB |
| MiniMax H3 | `hailuo3.0-t2v` / `-i2v` | 文生 / 首帧 | 🔴 `2200005 贝壳不足` |
| MiniMax H3 Max | `hailuo_h3_max_t2v` / `-i2v` | 文生 / 首帧 | 🔴 `2200005 贝壳不足` |

产物全部在 `e2e-output/video/`（按形态前缀命名）。

⚠️ **未实跑**：`veo3.1` / `sora2` / seedance2.0* 各支（用户本轮未授权）。
⚠️ **未覆盖的形态**：任何族的**多参考图**（`all-reference`，上游最多 9 张）——
本服务只实现 1 首帧 + 1 尾帧，传多了在本地 400。

🔴 **H3 / H3 Max 的 `贝壳不足` 不是余额问题**：紧接着跑 `23217` **成功**
⇒ 账户有额度，而这两族对**免费账户（`vipInfo.type=0`）不开放**。
⚠️ 参照数据：计价表里 `hailuo3.0*` 的 `realCost` 全是 `0` —— 那是**未定价**，
**不是**"免费"（曾据此误判，实测打脸）。

**唯一没验证过的形态**是 `23218` 的**首尾帧** —— 上游 `Hailuo 2.3-Fast` 组没声明
`end_frame`，本服务已在本地 400 拦下（不会去打上游）。

产物在 `e2e-output/video/`（`t2v-*.mp4` / `first-*.mp4` / `pair-768p-*.mp4`）。
`usage.forecast_credits` 与上游计价表逐条对上（15 / 25）。

**失败样本（同样是资产）**：

| 组合 | 结果 |
|---|---|
| `23210` 首帧+尾帧 · **512p** · 6s | ❌ `code 2400001` —— **512 档未声明 `supportFrame`**（本服务现已拦截并改档） |
| 合成渐变图 vs 真实照片 | 两者以同一码失败 ⇒ **失败与帧素材内容无关**（已排除该假设） |

⚠️ `23210` 那次的**产物 URL 是靠真实批次重放取回的**（当时轮询路径还是错的），
`23218` / `23204` 两次则是**实时一轮跑完**（POST → 轮询 → 取 URL → 下载）。
两条证据合起来才覆盖"建任务"与"收口"两半。

⚠️ 一次视频最高 180 积分，**任何真实提交都必须由人显式发起**。

---

## 5.2 账户状态与"余额不足"（🔴 实测）

视频侧余额不足回的是 **`code 2200005 贝壳不足`** —— 注意：文案里**没有**"积分/额度/余额"
任何一个词（视频侧把积分叫**贝壳**）。本服务已按**码 + 文案**双重识别并归入
`upstream_quota_exhausted`，错误文案明确写「重试无用，请充值」。

**只读的账户状态端点**（零计费，已实测可用）：

```
GET /v1/api/user/info      → data.userInfo.{name,userID,vipInfo.type,retentionDays,…}
```

`vipInfo.type = 0` ⇒ **免费账户（非会员）**；`expireTime = 0` 同理。
⚠️ 该响应里**没有**余额字段 ⇒ 本服务**无法**在提交前预知贝壳够不够，
只能靠上游那一次拒绝。（图片侧同理。）

## 6. 刻意不做的（"没做"是决定，不是遗漏）

| 项 | 为什么 |
|---|---|
| `veo3.1-s2v` / `veo3.1-s2v-fast` / `23021` | `type=s2v` 的**多参考图 / 主体参考**语义与"首帧/首尾帧"不是一回事，未取证 ⇒ 传了 **400** |
| `hailuo3.0-extend` | **视频延长**需要传参考视频，本服务不接受视频素材 |
| 多参考图（`all-reference`，Hailuo 3.0 / Seedance 2.0 最多 9 张） | 帧语义未取证 ⇒ 只接受 1 首帧 + 1 尾帧 |
| 音频参考（`generate_audio` / 音频素材） | 音频上传与字段形态未取证 |
| `callback_url`（webhook） | 没有回调通道，不复刻上游的回调重试语义 |
| 真实上游并发/频控阈值 | 未取证；闸门沿用图片链路的保守默认 |
| 其余模型的端到端实测 | 已实测 `23204` / `23218` / `23210`（默认族三槽位）。其它族（`veo3.1` / `sora2` / `hailuo3.0` / `hailuo_h3_max_*` / seedance2.0*）**未实跑**：档位与单价来自公开端点，创建 body 同构，但**各自可能有自己的前置校验**（正如 512 档那次）⇒ 首次使用某族前请先看受理日志的降级留痕并预留一次失败预算 |
