# 02 宿主侧设计：站点代请求（已实现）

> 本文档描述在**主程序仓库**（`G:\Github\movieclaw`）的改动。**实现已完成**（2026-10-18），
> 与原设计有一处简化：**不新增 `SITE_PROXY` 服务键**——插件经既有 `HOST_OPS` 操作链路调用
> `site.proxy.request` 即可，少暴露一个进程内服务面，授权/审批/审计全部复用现成机制。

## 为什么是「代请求」而不是「开放会话」

直接开放 `SITE_ACCESS`（已认证 BaseSite）给第三方 = 插件能拿 cookie 明文、能任意调
`download_torrent`、能枚举站点全部数据——危险面不可收口。主仓库设计文档自己也把它列为缺口
（「主要缺口：站点能力开放给插件」）。

「代请求」（proxy）把信任边界收窄成：**插件声明意图，宿主执行，回传脱敏响应**。
宿主在中间做校验（站点已配置、站内相对路径、每站限流），凭据永远不离开宿主。

## 已实现的内容

### 宿主操作 `site.proxy.request`（`src/movieclaw_api/api/routes/sites.py`）

```
POST /api/v1/sites/{site_id}/proxy
```

请求体（`SiteProxyRequest`，schemas/site.py）：`method`（GET/POST）、`path`（站内相对路径）、
`base`（web/api 域名二选一）、`headers`（插件补充的非敏感头）、`body`（dict 按 JSON / str 按原文）、
`timeout`（1–120s）。

响应（`SiteProxyResponse`）：`status_code`、`headers`（安全子集，剥 set-cookie/www-authenticate）、
`text`（512KB 截断，`truncated` 置真）、`json_body`（JSON 响应的解析结果）、二进制内容不回传。

### 安全机制（全部带单测）

| 机制 | 实现 |
|---|---|
| 防 SSRF | `proxy_path_error` 挡 `//`、`scheme://`、反斜杠、CRLF；`proxy_same_origin` 拼接后按 scheme+host 复核（最终防线）；完整 URL 一律由宿主按站点注册域名拼 |
| 凭据不出宿主 | 请求头黑名单（cookie/authorization/x-api-key/host）在 `sanitize_proxy_headers` 剥除，认证头由宿主会话附加且优先；响应剥凭据相关头 |
| 认证免维护 | 经 `SiteAccessManager.get()` 共享会话：Cookie/API-Key 自动附加、过期自动重认证、1800s TTL |
| 限流/代理 | 复用 tracker `HttpClient`（site:<id> 每站最小间隔 + egress 代理标签），与宿主自身站点流量共享节流 |
| 错误语义 | 凭据不可用 → 409（中文原因）；路径/头不合规 → 400；未知站点 → 404；超时/网络 → 503；对插件表现为结构化 `OpsError` |

### 随带发现并修复的宿主 Windows bug

- **`_ENV_KEEP` 缺 SystemRoot**：子进程环境只保留 `PATH/LANG/LC_ALL/TZ/HOME/TMPDIR`，
  Windows 上剥掉 `SystemRoot` 会让子进程 winsock 初始化失败（getaddrinfo 报 WinError 10106）。
  已修复：按大小写不敏感匹配并保留 `SystemRoot`（`plugin_runtime.py`）。
- **`AF_UNIX` 平台缺口（未修，独立工程）**：进程外插件的 IPC 传输层
  （`plugin_runtime._listen`、`plugin_gateway`、SDK `UnixHttp`/`open_unix_connection`、fd 传递）
  全部建立在 Unix domain socket 上，而 Windows CPython 无 `socket.AF_UNIX`——
  **进程外插件在 Windows 宿主上从未能运行**（官方 `test_plugin_packages.py` 在 Windows 同样全挂）。
  需要引入命名管道 / TCP 回环替代传输层，是独立的宿主工程；Linux（NAS/CI）不受影响。

## 验证状态

- 单测 `tests/api/test_site_proxy.py`：26 个用例全绿（路径校验、头黑名单、截断、
  409/400/404/503 语义、web/api 域名选择）。
- sites/tracker 回归：115 通过。
- uidev 真实服务器 + 插件联合验证：见 04-验收（失效链路端到端成功）。
