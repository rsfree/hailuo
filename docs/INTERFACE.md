# 对外契约（冻结）· 图片链路

> 本文件是**图片链路唯一的对外契约真相**。改动 = 破坏调用方，必须同步 `tests/test_api.py`。
> 上游侧的字段、签名与错误码在 `docs/UPSTREAM.md`；推导过程在 `.workbuddy/memory/`。
>
> 📹 **视频链路是另一份契约**：火山方舟 Seedance 原生协议，
> 端点 `/api/v3/contents/generations/tasks*` 与 `/v1/models`，
> 见 **[`docs/SEEDANCE.md`](SEEDANCE.md)**。两条链路同进程、**不同表**、互不影响。

---

## 0. 端点

| 方法 | 路径 | 状态码 | 用途 |
|---|---|---|---|
| `POST` | `/async/v1/images/generations` | `202` | 受理，**只回一个 `task_id`** |
| `GET` | `/async/v1/images/generations/{task_id}` | `202`/`200` | 非终态回排队态；终态回结果 |
| `GET` | `/async/v1/images/generations` | `200` | 本 Key 的任务列表 |
| `DELETE` | `/async/v1/images/generations/{task_id}` | `200`/`400` | 删除**已终态**的任务 |
| `GET` | `/async/v1/models` | `200` | 能力清单（OpenAI 形态 + 上游模型注册表） |

运维端点（**不属于对外契约**）：`GET /healthz`（零依赖，容器探活用）、
`GET /readyz`（只 ping 一次库 —— 凭据随请求来，探针不替上游判 token 有效性）、`GET /stats`、`GET /capabilities`。

鉴权（**透传**，2026-09-21 起唯一方式）：`Authorization: Bearer <你的 hailuo 登录 token>`。

- 该 token **同时就是上游凭证** —— 任务全程（建任务/轮询/OSS 上传）都用它
  ⇒ **费用记在 token 所有者账上**；服务端**不持有任何账号凭据**，
  因此也不存在"白名单 key"或"服务端账号"这一档（无向后兼容包袱）。
- 本地只校验**结构与 `exp`**（**不验签**：没有签名密钥）；伪造/过期 token
  由**上游**兜住（会拿 401）—— 本地这层只为"早失败、少一次往返"。
- 明文 token **绝不落库**（库中只有指纹）；它驻留进程内存池，
  进程重启/TTL 过期后在途任务会以 `503 credential_unavailable` 明确失败。
- 任务与**凭据指纹**绑定（不同 token 的任务互相看不见）。

---

## 1. 受理

```http
POST /async/v1/images/generations
Authorization: Bearer <你的 hailuo 登录 token>
Content-Type: application/json

{
  "model": "hailuo-i2i",
  "prompt": "把背景换成雪山，保留人物光影",
  "image": ["https://…/ref.png"]
}
```

**响应 `202`**：

```json
{ "task_id": "hailuo_b8d9f0b8247f4eeda60f84c908e192cb" }
```

`Location: /async/v1/images/generations/{task_id}`

🔴 **只有 `task_id` 一个键。** 不返回状态/时间戳之类的附加信息 ——
调用方要的是"拿着它去轮询"。多一个键就是契约变更。

**请求内零上游往返**：建任务（计费动作）交给后台协调器，受节奏闸门约束。
输入图拉取与上传同样在后台 ⇒ 上游网络抖动体现为任务 `failure`（附原因），
而不是让受理请求跟着一起抖。

### 请求字段

| 字段 | 必需 | 说明 |
|---|---|---|
| `model` | 否 | 见 §3。留空 ⇒ 按 `image` 是否为空推导 i2i / t2i（**并在 `degradations` 留痕**） |
| `prompt` | 是 | 提示词。i2i/t2i 都必需 |
| `image` | i2i 必需 | **数组**。文生图传 `[]` 或省略；传字符串会被明确拒绝并给出正确写法 |
| `n` | 否 | 出图张数。默认 **1**，上限 10（超出会吸附并留痕） |
| `size` | 否 | `"2048x2048"`。**本服务换算**成上游的 `resolution` + `aspectRatio`，**必留痕** |
| `resolution` | 否 | 原生档位（`1K`/`2K`/`4K`），按**该模型声明的枚举**校验 |
| `aspect_ratio` | 否 | 原生比例（如 `1:1`/`16:9`/`Auto`），同样按模型声明校验 |
| `quality` | 否 | 仅 `gpt-image-*` 支持（`low`/`medium`/`high`/`xhigh`/`max`） |
| `reference_mode` | 否 | 上游 `referenceMode`。**默认不发送**（与抓包一致） |
| `seed` | 否 | 整数（上游未声明该字段，传了会被忽略并留痕） |
| `negative_prompt` | 否 | 上游未声明该字段，传了会被忽略并留痕 |

### `image` 是数组，张数上限**按所选模型自己声明的**

**每一项的两种形态**（可混用）：

| 形态 | 写法 | 适用 |
|---|---|---|
| 公网 URL | `"https://…/a.png"` | 图已在可访问的位置 |
| **内联 base64** | `"data:image/png;base64,<b64>"` | **本地图片**（HTTP 接口不收裸文件 ⇒ 调用方 base64 编码即可，无需先传公网） |

⚠️ data URL 的规则与 URL 完全一致：**仍然按魔数嗅探真实格式**（`data:image/jpeg` 里装 PNG ⇒
`400`，不静默改判）、仍然受 `MAX_DOWNLOAD_BYTES` 体积上限约束、仍然要真实上传到上游后才垫图。

| 上游模型 | 上限（`maxSupportImageCount`） |
|---|---|
| `gpt-image-2` / `gpt-image-2.5-sunburst` / `gpt-image-2.5-flare` | **16** |
| `nano_banana21_flash` / `nano-banana2` / `seedream-5.0` / `seedream-4.5` / `mj_v7` / `mj_niji7` | **14** |
| `gpt-image-1.5` | **3** |
| `image-01` | 未声明（不校验） |

- 🔴 **三种行为只有两种**：要么**全部接受**（全部上传、按请求顺序进 `fileList`），
  要么**明确拒绝**。**绝不存在第三种（收下 N 张却只用第 1 张）**。
- 超限一律 `400 invalid_parameter`（`param="image"`），信息里写明上限与应有做法。

⚠️ **参考图模式**：`common_config` 给每个模型声明 `mode`。本服务实现的是
**`image-reference`**（图片参考/编辑）—— 11 个模型里 10 个是它；
**`image-01` 是 `subject-reference`（主体参考）**，带 `image` 会 100%
被上游拒（实测 `code 2400052`）⇒ 本服务**在受理阶段就返回 400** 并说明原因
（`image-01` 仍可正常走文生图）。

### 提交失败的自动重试（**仅限"建任务之前"**）

输入图下载/上传、连接抖动等**发生在建任务之前**的瞬时失败，上游**一定没有**建任务
⇒ 协调器会保留任务在 `queued` 并自动重试，上限 `SUBMIT_MAX_ATTEMPTS`（默认 3）。
已发出建任务请求之后的失败**绝不重试**（无法排除上游已受理 ⇒ 会是重复计费）。
超过上限 ⇒ 任务进 `failure` 并带原因。

### 关于「认得但做不到」的字段

出现 `watermark` / `response_format` / `style` / `stream` / `user` /
`sequential_image_generation` / `max_images` / `background` / `output_format` /
`moderation` 时**不报错**，而是进 `degradations`（见 §5）。其它未知字段 → `400`。

这条区分的判据是：**这是"上游没有"还是"你写错了"？**
两者的修复动作完全不同 —— 把两者都塞进"不支持"，会让人去查上游能力表，
而真正的问题在请求体。

---

## 2. 查询

```http
GET /async/v1/images/generations/{task_id}
```

**不需要 Authorization**（见 §2.4）。

### 2.1 非终态 → `202`

```json
{ "task_id": "hailuo_...", "status": "queued" }
```

`status ∈ {queued, in_progress}`。**202 的意思是"还没好，继续轮询"** ——
不要把排队态当结果。

### 2.2 成功 → `200`

```json
{
  "data": [ { "url": "https://cdn.hailuoai.video/moss/…/xxx.png" } ],
  "created": 1789923012,
  "usage": { "images": 1, "forecast_credits": 8 }
}
```

三条刻意的取舍：

1. **`data[]` 里只有 `url`。** 宽高/文件名我们确实知道，但**不塞进来** ——
   与冻结契约逐字一致，多一个键就多一分"形状不同"的风险。那些真知识在 trace 里。
2. **结果 URL 是上游直链，原样透传、不做转存。** 返回的是**去水印**那条
   （`downloadURL.withoutWatermarkURL`），取不到时才退回带水印的 `url`。
   ⚠️ 链接有效期**未取证** —— 需要长期可用链接时得另做转存，本服务没做。
3. **`usage.forecast_credits` 是「预估」。** 它来自上游计价表
   （`imageModels[].costs[].realCost`），**不是账单实扣值**；
   查不到该 (resolution, quality) 组合时**不给这个键**（不编数字）。

### 2.3 失败 → `200`

```json
{
  "task_id": "hailuo_...",
  "status": "failure",
  "error": { "message": "...", "type": "...", "code": "..." }
}
```

**失败也回 200**：任务本身完成了（只是结果是失败），请求没出错。
回 4xx 会让调用方的重试逻辑误触发。

⚠️ 任务失败**不代表没花钱**：建任务是计费动作。`code="task_failed"` 的文案里
会明确写出来。

### 2.4 不存在 / 不属于本 Key → `404`

```json
{ "error": { "message": "任务 ... 不存在，或不属于当前 API Key。",
             "type": "invalid_request_error", "code": "task_not_found" } }
```

刻意**不区分**这两种情况（区分开等于告诉攻击者"这个 id 是存在的"），
且**本地拦死、不发上游请求**。

鉴权语义（三种情况分得很清，刻意不合并）：

- **完全没带** `Authorization` ⇒ **放行**。理由：`task_id` 是不可猜的 128 位随机值，
  且只在受理时返回给带凭据的调用方 ⇒ **id 本身就是凭据**（可以把结果链接直接给别人看）。
- **带了但无效** ⇒ **照旧 `401`**。不能因为"反正放行"就把错的凭据蒙过去 ——
  那会让调用方的配置错误被静默吞掉，是最难查的一类问题。
- **带了且有效、但不是该任务的属主** ⇒ **照样能查**（与"没带"同一待遇，语义统一好预测）。

⚠️ **列表与删除接口仍然强制鉴权** —— 否则可以枚举/删除别人的任务。

---

## 3. 能力与 `model` 取值

### 3.1 本服务的能力名

| `model` | 能力 | 输入图 | prompt |
|---|---|---|---|
| `hailuo-i2i` | **图生图（本项目重点）** | **1..N**（N 由模型上限决定） | 必需 |
| `hailuo-t2i` | 文生图 | 0（传 `[]`） | 必需 |
| `hailuo-image` | 自动：按 `image` 是否为空推导 | 任意 | 必需 |

**别名**（**大小写不敏感**）：

- i2i：`i2i` / `image2image` / `img2img` / `图生图` / `垫图` / `参考图` /
  `hailuo` / `hailuo-image` / `image` / `edit` / `修图` / `改图`
- t2i：`t2i` / `text2image` / `txt2img` / `文生图` / `生图`
- 通用：`auto`

**占位名**（`dall-e-3` / `gpt-image-1` / `seedream` / `flux` / `sdxl` …）
等价于"没写 `model`"，走默认推导 —— 第三方 SDK 常硬编码这些值，
它们不代表调用意图。

### 3.2 上游模型注册表（11 个，`GET /async/v1/models` 可查）

运行期从**两个公开端点**实读（零成本、免鉴权），读不到时退回冻结快照
（`models.SNAPSHOT_DATE = 2026-09-21`）并留降级痕（`/stats` 的
`capabilities.source` 会显示 `frozen_snapshot`）。

**模型别名**（与枚举值等价、零映射说明；精确匹配）：
`nano-banana-2` = `nano_banana21_flash` · `nano-banana-pro` = `nano-banana2` ·
`midjourney-v7`/`mj-v7` = `mj_v7` · `midjourney-niji7`/`mj-niji7` = `mj_niji7` ·
`image-1.0` = `image-01` · `seedream-5.0-lite` = `seedream-5.0`。
⚠️ `nano-banana-2`（flash）与 `nano-banana2`（pro）只差一个 `-`。

| 上游模型 key | 展示名 | 分辨率档 | 比例档 | 单价（积分） |
|---|---|---|---|---|
| `nano_banana21_flash` | Nano Banana 2 | 1K/2K/4K | Auto + 14 种 | 4 / 5 / 8 |
| `nano-banana2` | Nano Banana Pro | 1K/2K/4K | Auto + 10 种 | 6（1K/2K） / 10（4K） |
| `seedream-5.0` | Seedream 5.0 Lite | 2K/4K | Auto + 10 种 | 4 |
| `seedream-4.5` | Seedream 4.5 | 2K/4K | Auto + 10 种 | 4 |
| `gpt-image-2` | GPT Image 2 | 1K/2K/4K | 10 种（无 Auto） | 2..160（按 quality） |
| `gpt-image-2.5-sunburst` | GPT Image 2.5 Sunburst | 1K/2K/4K | Auto + 10 种 | 2..180（按 quality） |
| `gpt-image-2.5-flare` | GPT Image 2.5 Flare | 1K/2K/4K | Auto + 10 种 | 2..180（按 quality） |
| `gpt-image-1.5` | GPT Image 1.5 | Low/Medium/High | Auto/1:1/3:2/2:3 | 4 / 8 / 15 |
| `mj_v7` | Midjourney V7 | 未声明 | Auto + 10 种 | 3（`defaultCost`） |
| `mj_niji7` | Midjourney Niji7 | 未声明 | Auto + 10 种 | 3（`defaultCost`） |
| `image-01` | Image-1.0 | 未声明 | 6 种 | 1（`defaultCost`） |

**默认模型** = `nano_banana21_flash`。依据：它是本项目**唯一有端到端抓包证据**的
图生图模型（1 张参考图 + 指令 → 4096×4096 PNG）。
⚠️ 它**不是最便宜**的（`image-01` 固定 1 积分），要更省请显式传 `model`。
未指定时会在 `degradations` 里写明"用了默认"。

### 3.3 刻意缺席的能力

**视频链路不注册。** 上游的模型信息端点一并返回 24 个 `videoModels`，
但**本服务只做图片** —— 视频链路未取证，不做假能力。
`DELIBERATE_ABSENCES` 里写明了这一点，`/capabilities` 会把它带出来。

---

## 4. 张数 `n` 的语义

- 不传 `n` ⇒ **1**。🔴 **不采用上游的 `defaultSelect` 张数** ——
  默认必须是**最省**的那个，否则调用方会按"1 张"的预期收到多张的账单。
- 显式传 `n`：`1..10` 原样生效；`>10` 吸附到 `10` 并**在 `degradations` 留痕**；
  `<1` 或非整数 ⇒ `400`。
- 上游 `quantity` 字段承载该值。

---

## 5. 降级留痕（只在日志与任务库，**不进 HTTP 响应**）

任何"请求了 A、实际做了 B"都会留痕。**2026-10-10 起接口响应不再携带
`degradations` 字段**，留痕只出现在两处：

· **服务日志**：受理时逐条打 `降级留痕：…`，输入图归一化打 `归一化留痕：…`（都带 task_id）；
· **任务库**：`degradations_json` 列（运维可直接查库）。

留痕内容示例（受理日志形态）：

```
降级留痕：未指定上游模型（model='hailuo-i2i'，能力 hailuo-i2i）⇒ 使用默认 nano_banana21_flash。
降级留痕：size=4096x4096 ⇒ resolution=4K（本服务按长边 4096 换算；上游只认 ['1K','2K','4K'] 这类档位）。
归一化留痕：输入图被缩放（…）。
```

来源有四类：**参数换算/吸附**、**默认值代入**、**输入图归一化**、**能力表读取失败退回快照**。

🔴 **静默降级 = 让人按 A 的预期为 B 付钱。** 凡会改变结果或花费的取舍都必须留痕 ——
接口字段收敛后，**日志就是留痕的对外出口**，排查以日志为准。

---

## 6. 错误信封

```json
{ "error": { "message": "...", "type": "...", "code": "...",
             "param": "可选", "retry_after": 可选, "detail": "可选" } }
```

| `code` | HTTP | 含义与**下一步** |
|---|---|---|
| `invalid_parameter` | 400 | 请求写错了。`param` 指出是哪个字段 |
| `content_policy_violation` | 400 | 上游送审/版权拦截 ⇒ 换 prompt 或换图 |
| `task_not_deletable` | 400 | 未终态的任务不能删（上游没有取消端点） |
| `invalid_api_key` | 401 | 调用方的 Key 不对 |
| `task_not_found` | 404 | 不存在或不属于本 Key |
| `upstream_rate_limited` | 429 | 上游限流，**可退避重试**（带 `Retry-After`） |
| `upstream_quota_exhausted` | 429 | 额度/积分耗尽，**重试无效** |
| `risk_control_challenge` | 429 | 命中风控，**重试会延长标记**，服务已进入冷却 |
| `upstream_error` | 502 | 上游 5xx / 非 JSON / WAF 页 |
| `upstream_not_configured` | 503 | 服务未配 `HAILUO_TOKEN`（**部署问题**，不是你的错） |
| `capability_unavailable` | 503 | 能力表不可用且无冻结快照 |
| `upstream_timeout` | 504 | 上游超时 |

`Retry-After` **只在是真的才知道**的时候给 —— 编一个数字等于伪造事实。

---

## 7. 删除

| 任务状态 | `DELETE` 行为 |
|---|---|
| 非终态（`queued` / `in_progress`） | **`400`** —— hailuo **没有取消端点** |
| 终态 | `200 {"task_id": "...", "status": "DELETED"}`，删掉本地记录 |

🔴 未终态任务的删除**必须响亮失败**。本地置"已取消"就返回成功有三个后果：
① 上游任务继续跑、继续计费，而调用方以为停了；② 本地与上游状态永久不一致；
③ 没有任何出口能看出来。

---

## 8. 测试与运行

```bash
python -m pytest -q          # 215 用例，**零真实上游调用**
```

- **零真实上游调用**：所有用例注入假上游（`httpx.MockTransport` 覆盖全部端点），
  一个字节都不发出去。建任务是计费动作，这条红线由夹具保证。
- **缺依赖就响亮失败，不静默跳过** —— 跳过会让人把"没跑"当成"跑过了"。
- 纯离线用例（签名向量 / 能力解析 / 可观测性 / 接线门禁 / 媒体层）不需要数据库。

### 🔴 启动冒烟必须关协调器

```bash
COORDINATOR_ENABLED=0 gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

协调器默认开启，而**建任务是计费动作**。做"服务能不能起来"的冒烟时如果不关它，
它会代替你向上游提交任务。
