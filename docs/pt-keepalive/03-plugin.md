# 03 插件设计（plugins/pt-keepalive/）

> 实现于 2026-10-18，2026-10-18 复查后修订。以代码为准，本文记录结构与判据。

## 目录结构（本仓库）

```
plugins/pt-keepalive/
├── movieclaw-plugin.toml        # 清单（extra=forbid，字段收着写）
├── store.json                   # 本仓库索引元数据（作者/主页/许可证=见仓库许可证）
├── README.md                    # 面向用户：做什么、设置、通知、风险
├── CHANGELOG.md
├── pt_keepalive/
│   ├── __init__.py              # @plugin 入口 + apply()（只登记）
│   ├── events.py                # 事件与载荷（清单 [[events]] 一一对应）
│   ├── settings.py              # Settings(pydantic)：总开关/签到开关/日报开关
│   ├── runner.py                # 保活轮：site.list → 逐站适配 → 状态机 → 事件
│   ├── result.py                # 结果模型 + 证据判定纯函数（可单测）
│   ├── sites/__init__.py        # 分发表 + NexusPHP 模板 + M-Team 专用
│   └── state.py                 # PLUGIN_DATA 状态机读写
└── tests/
    ├── test_result.py           # 判定正反例 + 状态机
    └── test_runner.py           # 适配器 + mock 宿主操作的整轮
```

零 vendor、零 `[dependencies]`：只用宿主自带库（`parsel` 用于剥 script/hidden 后的证据匹配；
`pydantic` 用于设置与事件载荷；`fastapi` 用于状态路由）。包体约 26 KB。

## 清单（movieclaw-plugin.toml）要点

```toml
[requires]
"scheduled-tasks" = "^1.0"
"plugin-events" = "^1.0"
"plugin-data" = "^1.0"
"plugin-routes" = "^1.0"

[permissions]
operations = ["site.list", "site.catalog", "site.proxy.request"]
```

- `site.list`：拿已配置且 `usable` 的站点。
- `site.catalog`：`site.list` 不带展示名，通知文案用 catalog 的 `display_name`（拿不到退回 site_id）。
- `site.proxy.request`：唯一发站点请求的通道。
- **不声明 `network = true`**：插件没有任何直连出网（所有请求经宿主会话），声明它只会让用户在
  安装页看到一个用不到的网络权限。
- 三个事件：`auth.expired`（失效，active）、`report.daily`（日报，passive，默认关）、
  `report.failures`（连续失败升级，active，默认开）。

## apply()：只登记

```python
@plugin(
    "pt-keepalive",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_EVENTS, PLUGIN_ROUTES),
    permissions=("site.list", "site.catalog", "site.proxy.request"),
    config=Settings,
)
async def apply(ctx: Context) -> None:
    # 1) 自检：HOST_OPS 目录里有 site.list + site.proxy.request 吗？没有 → 记日志、不登记任务
    # 2) 登记三个事件，拿 Publisher
    # 3) get /status 路由（admin 区）
    # 4) 贡献定时任务 pt-keepalive.run
```

- 自检只要求 `site.list` + `site.proxy.request`（真正必需的）；`site.catalog` 缺了只是通知里
  显示 site_id，不降级整个插件。
- 定时任务默认 CRON `17 9 * * *`；用户在「设置→定时任务」改，配置模型里不放间隔。
- 30 秒上限内完成登记；保活轮本体是任务处理器，跑多久都行。

## 站点适配框架

### 分发表

```python
SPECIAL_ADAPTERS = {"mteam": mteam_adapter}

def adapter_for(site: SiteInfo) -> Adapter | None:
    special = SPECIAL_ADAPTERS.get(site.site_id)
    if special is not None:
        return special
    if site.auth_type == "apikey":   # sunnypt 等纯 API 站
        return None                   # 本轮跳过，不拿 HTML 判定器读 JSON
    return nexusphp_adapter
```

`site.list` 没有 `framework` 字段，分发只能按 `site_id` 白名单 + `auth_type` 兜底：
纯 API 站（`apikey`）没有可判定页面，**未适配就跳过**，不冒充失败。宿主 26 站里
mteam/sunnypt 是 API 框架，其余是 NexusPHP 页面。

### 步骤协议

```python
@dataclass
class Step:
    request: PlannedRequest   # method/path/base/headers/body
    judge: Callable[[StepResponse], SiteResult]
    signin_only: bool = False # 关了签到就整步跳过（连请求都不发）
```

- `nexusphp_adapter`：
  1. `GET index.php`（base=web）——确认登录态：登出链接 = 在线，密码框 = 失效；
  2. `GET attendance.php`（`signin_only=True`）——尝试签到。
  分两步是因为签到页不是每站都有；先确认登录态，签到页 404 只判「登录在线」。
  签到页判定用 `login_confirm=False`（该页通常没有导航栏，不要求登出链接）。
- `mteam_adapter`：`POST /api/member/updateLastBrowse`（base=api，`_timestamp`+`_sgin` 签名，
  `Did/visitorid` 由 site_id 稳定派生，不每轮换设备）。

任一步判 `FAILED` / `NEED_HUMAN` 即停止后续步骤。

## 结果判定（result.py，纯函数、单测重点）

```python
class Outcome(Enum):
    SIGNIN_OK = "signin_ok"     # 签到成功（含「今天已签」幂等）
    LOGIN_ONLY = "login_only"   # 登录有效，签到未确认/未做
    FAILED = "failed"           # 请求失败或证据不明
    NEED_HUMAN = "need_human"   # CF/WAF/验证码/答题，不算失败
    SKIPPED = "skipped"         # 本轮没访问（未适配/签到关闭），不算失败
```

判定纪律（照抄 MoviePilot 踩坑经验，全部纯函数可测）：

1. 先剥 `<script>/<style>/hidden/aria-hidden` 再匹配文本（防模板文本冒充提示）；
2. **登录失效证据优先**：出现密码输入框/登录表单 → `auth_expired`；
3. Cloudflare challenge 特征只看 `<title>` 与**可见**文本 → `need_human`
   （站点自己的 JS 里有 "just a moment" 不算）；HTTP 401/403 且无 challenge 特征 → `need_human`
   （WAF/IP 限制可能性大于凭据失效，不误报告警）；HTTP 5xx → `FAILED`；
4. 签到成功：可见文本命中成功关键词，且没有否定提示；
5. 「今天已签/请勿重复」→ 幂等成功；明确的否定提示（「回答错误」）优先于成功词；
6. 都不认识 → `LOGIN_ONLY`（保守），绝不虚报 `SIGNIN_OK`。

API 判定：先看成功组合（含 `{code:"1",message:"FAIL"}` 反直觉形态），再看 HTTP 401/403 或
`message` 里的失效特征；宿主没解析 `json_body`（站点漏标 content-type）时从 `text` 再解析一次。

## 状态机（state.py，PLUGIN_DATA）

单键 `keepalive` 存全部站点（<64KB）：

```python
{sid: {outcome, detail, site_name, consecutive_failures,
       last_ok_at, alerted_expired, failure_alerted}}
```

- `SIGNIN_OK`/`LOGIN_ONLY` → 清零失败、清两个告警位；
- `FAILED` → 计数 +1；**连续 3 轮**进失败明细；
- `auth_expired` → 失败计数直接置阈值（失效必须让用户看见），发一次告警；
  **告警发送失败时不置位**，下一轮重试；
- `NEED_HUMAN`/`SKIPPED` → 只更新展示字段，不计数。

## 事件与通知

| 事件 | 触发 | 通知 | 默认 |
|---|---|---|---|
| `pt-keepalive.auth.expired` | 凭据失效（同一失效期一次） | 「{site_name} 登录已失效」 | 开（active） |
| `pt-keepalive.report.daily` | 每轮汇总（`daily_report=true`） | 「PT 保活日报：N 站在线」+ `{detail}` | 关（passive） |
| `pt-keepalive.report.failures` | 有站点连续 3 轮失败，每段失败只发一次 | 「N 站连续失败」+ `{detail}` | 开（active） |

通知模板只做字段替换、不拼接数组，所以 `DailyReport.detail` 在插件里先拼成一行；
`failures` 保留结构化形式给 Webhook / 别的插件。日报默认关闭，失败升级因此走
单独一条默认开的事件——否则关掉日报的用户永远看不到失败。

## 设置模型（settings.py）

```
enabled: bool = True          # 总开关（False 时任务跑空转，直接返回）
signin_enabled: bool = True   # 签到动作开关（False → 只访问首页确认登录态）
daily_report: bool = False    # 日报开关（失效/失败告警不受它控制）
```

不设「站点选择」：启用范围 = 宿主已配置且 usable 的站点全集。

## 路由（PLUGIN_ROUTES）

`GET /status` → 各站最近一轮结果（名称/结果/说明/连续失败数/上次成功/更新时间），
`operation_id="plugins.pt-keepalive.status"`（成为 `mclaw plugins pt-keepalive status`）。
返回类型 `dict[str, Any]`，模块顶部真实导入，满足进程外路由的注解要求。

诊断页：apply 自检 + 任务贡献 + 每轮日志（`ctx.logger`，中文，带 site_id）。
