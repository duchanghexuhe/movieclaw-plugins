# 电视桌面（tv-home）

把安卓电视 / 盒子 / 投影的厂商桌面换成「MovieClaw 桌面」：开机、按主页键直达 MovieClaw 首页，
从 MovieClaw 退出是「全部应用」。不 root、不动系统分区，改设备前写状态记录、每一步可还原。

设计与规格在主仓库 `docs/design/androidtv-home.md`；本插件是其中「电视桌面」插件的部分（§8），
P1 范围：`guide` `connect` `probe` `install` `home` `verify` `restore` `close`。
`debloat`（去臃肿）、`report`（摸底报告）与机型记录是 P2。

## 接口

都挂在管理员区（`/api/v1/plugins/tv-home/…`），agent 经 `mclaw plugins tv-home …` 调用：

| 接口 | 作用 |
|---|---|
| `GET /guide` | 给 agent 的操作说明 |
| `POST /connect` | 连接电视；第一次连时电视上弹「允许调试」，返回 `waiting_auth`，用户点完再调一次 |
| `POST /probe` | 读型号、固件、内存、HOME 候选与优先级、已装的 MovieClaw / 桌面壳版本 |
| `POST /install` | 装桌面壳；MovieClaw 主 App 用 `main_apk_url` 下载（默认最新正式版，GitHub digest 或 `main_apk_sha256` 校验） |
| `POST /install-apk` | 同上，MovieClaw 主 App 用请求体直接上传（octet-stream，可选 `sha256` 查询参数校验） |
| `POST /home` | 换桌面：写记录 → 装桌面壳 → 单独启动验证 → 设默认 →（需要时）停用厂商桌面 → 注入主页键验证 |
| `POST /verify` | 汇总现状：HOME 归属、HOME 角色、被停用的包与记录是否一致 |
| `POST /restore` | 按状态记录还原：先启用厂商桌面、设回原桌面，再卸桌面壳（顺序不能反） |
| `POST /close` | 请用户关掉调试后调用；确认 5555 拒绝连接（只认「连接被拒绝」） |

## 目录

- `tv_home/`：插件代码。`operations.py` 是同步的设备操作逻辑（单测直接测它），
  `adb.py` 封装 adb_shell，`routes.py` 把接口挂出来。
- `launcher/`：桌面壳「MovieClaw 桌面」的源码（独立 Gradle 工程，编译输出在隐藏的 `.build/`）。
- `apk/MovieClawHome.apk`：**不入库**。发版流程先编出并签名桌面壳，放进这个位置再打包
  （P1 未接 CI 构建，手动放入即可）；没有这个文件时 `install` 会明确报错。
- `requirements.lock`：由 `PYTHONPATH=<MovieClaw>/src python3 scripts/pack.py plugins/tv-home --lock` 生成，不手改。

## 依赖与出处

- adb 客户端用纯 Python 的 `adb-shell` 0.4.3（有全平台轮子），随插件依赖在安装时下载；
  `cryptography`（keygen 与签名要用）用主程序自带的。
- 换桌面顺序、防护规则与还原顺序沿用 [Gathin23/cleanyourtv](https://github.com/Gathin23/cleanyourtv)（MIT）踩出来的做法：
  不卸载只停用（`pm disable-user`）、还原先启用厂商桌面再卸桌面壳、状态记录第一次不被覆盖、
  `pm install -r` 代替会卡在屏幕确认的 `adb install`。本仓库只借规则与格式，不依赖它的代码。
- `cmd package query-activities` 的输出解析按「`priority=N` 行 + 紧随的 `包名/类名` 行」的形状写，
  容忍大小写与多余空白；P1 真机验收（坚果 N1 / 模拟器）时以真实输出校对。

## 验证

```bash
python3 -m unittest discover -s plugins/tv-home/tests -t plugins/tv-home -v
python3 <MovieClaw>/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/scripts/check_plugin.py plugins/tv-home
python3 scripts/pack.py plugins/tv-home --output /tmp/tv-home-0.1.0.mcplugin
```

单测用假的 adb 链接（`tests/fake_adb.py`）覆盖：换桌面的七步与回滚、还原顺序、状态记录合并、
安装输出没有 `Success` 就停、`close` 只认「连接被拒绝」。装进真实服务器、按设计稿 §11 的 P1 验收
（模拟器逐条行为 + 真机）在单测之外。
