# 电视桌面（tv-home）

把安卓电视 / 盒子 / 投影的厂商桌面换成「MovieClaw 桌面」：开机、按主页键直达 MovieClaw 首页，
从 MovieClaw 退出是「全部应用」。不 root、不动系统分区，改设备前写状态记录，随时可还原。

## 使用前提

- 一台安卓 6.0 以上的电视 / 盒子 / 投影（Fire TV、三星 Tizen、LG webOS 不支持）；
- MovieClaw 服务器和电视在**同一个局域网**；
- 管理员账号（这个插件只能在管理员账号里用）；
- 电视的「设置」里能找到「关于」（用来打开开发者选项）。

## 怎么用（跟着 agent 走）

装好插件后，在 MovieClaw 里对 AI 助手说一句话（要点名插件）：

> 用电视桌面插件，帮我给客厅的电视换桌面

agent 会按「连接 → 摸底 → 安装 → 换桌面 → 验证 → 关调试」的顺序引导。你只需要做这几件事：

| 步骤 | 谁做 |
|---|---|
| 在电视上打开开发者选项和网络调试（agent 会把菜单路径念给你） | 你（遥控器） |
| 电视上弹出「允许调试吗」：选允许，并勾选「始终允许」 | 你 |
| MovieClaw 出现二维码：用手机扫码登录 | 你 |
| 换完桌面后按一下遥控器的主页键，告诉 agent 屏幕上是什么 | 你 |
| 最后在电视上关掉调试（agent 会确认端口已经关好） | 你 |

其余的事——找电视、摸底机型、下载校验安装包、装桌面壳、换桌面、验证、写还原记录——都是 agent 和插件做。
agent 每一步都会说清它在做什么、需要你做什么；出错会停下并原样转述原因，不会自作主张重试。

## 换完之后

- 开机、按主页键 → 直达 MovieClaw 首页；播放中按主页键 = 退出播放回首页（要等主 App 支持深链的发版）。
- 在 MovieClaw 首页连按两次返回 → 退出到「全部应用」，那里能打开系统设置和其它应用；在「全部应用」按返回不动。
- 遥控器的设置键、信号源键大多由系统直接处理，换桌面不受影响；个别机型的「应用」键行为，agent 摸底后会提示。

## 怎么还原

两种方式任选：

- **让 agent 还原**（推荐）：说「用电视桌面插件把电视还原」。插件按改设备前的记录恢复：
  先启用厂商桌面、设回原来的默认桌面，再卸载「MovieClaw 桌面」；MovieClaw 本体默认保留。
- **手动**：系统设置 → 应用 → 卸载「MovieClaw 桌面」。厂商桌面**没有**被停用的机型可以直接卸；
  被停用过的机型要先让 agent 跑还原（或到系统设置里把厂商桌面逐个启用）。

⚠ **顺序警告**：厂商桌面被停用的机型，如果先卸「MovieClaw 桌面」、厂商桌面还没启用，屏幕会黑、遥控器救不回来。
拿不准就让 agent 先跑还原。

## 这台机器能做到哪一步

摸底后 agent 会告诉你这台电视属于哪一级：

- **B 级（没验证过的机器，最常见）**：只换桌面——不停用任何应用、不去臃肿。
- **A 级（有验证记录的机型）**：换桌面 + 去臃肿（P2 起，跟着机型记录发版逐步覆盖）。
- **C 级（做不了）**：拒绝侧载、拒装桌面类应用、或主页键被厂商固定死的机器——插件不改设备、说明原因，并生成一份摸底报告（可以提交给项目改进通用流程，不会自动上传）。

## 常见问题

- **连不上（连接被拒绝）**：电视的网络调试没开，或电视和服务器不在同一个网段。
- **显示「等待授权」**：电视上弹了「允许调试吗」，点允许并勾选「始终允许」，然后让 agent 重试一次。
- **换桌面失败、判为 C 级**：插件已自动回滚、设备保持原样，并在服务器上留了救援命令；让 agent 把原因读给你听。
- **调试端口安全**：5555 是明文协议，所以最后一步务必关掉调试；插件只认「连接被拒绝」为已关闭。

---

## 给开发者的说明

规格在主仓库 `docs/design/androidtv-home.md`（§8 是本插件）；P1 范围是 `guide` `connect` `probe`
`install` `home` `verify` `restore` `close`。`debloat`（去臃肿）、`report`（摸底报告）与机型记录是 P2。

### 目录

- `tv_home/`：插件代码。`operations.py` 是同步的设备操作逻辑（单测直接测它），
  `adb.py` 封装 adb_shell，`routes.py` 把接口挂出来。
- `launcher/`：桌面壳「MovieClaw 桌面」的源码（独立 Gradle 工程，编译输出在隐藏的 `.build/`）。
- `apk/MovieClawHome.apk`：**不入库**。发版流程先编出并签名桌面壳，放进这个位置再打包；
  没有这个文件时 `install` 会明确报错。
- `wheels/`：锁文件里三个依赖的预编译轮子（与 `requirements.lock` 的 `#>` 行逐一核对哈希），
  服务器安装依赖时优先取这里，批准页照常按锁文件展示、安装零联网。
- `requirements.lock`：由 `PYTHONPATH=<MovieClaw>/src python3 scripts/pack.py plugins/tv-home --lock` 生成，不手改；
  仓库用 `.gitattributes` 钉它为 LF 行尾（CRLF 会让服务器的两处哈希对不上，见下）。

### 依赖与出处

- adb 客户端用纯 Python 的 `adb-shell` 0.4.3；`cryptography`（keygen 与签名要用）用主程序自带的。
- 换桌面顺序、防护规则与还原顺序沿用 [Gathin23/cleanyourtv](https://github.com/Gathin23/cleanyourtv)（MIT）踩出来的做法：
  不卸载只停用（`pm disable-user`）、还原先启用厂商桌面再卸桌面壳、状态记录第一次不被覆盖、
  `pm install -r` 代替会卡在屏幕确认的 `adb install`。本仓库只借规则与格式，不依赖它的代码。
- [pengchujin/aerial-tv](https://github.com/pengchujin/aerial-tv)（MIT）：`set-home-activity --user 0` 的写法、
  前台探测的 `mCurrentFocus` 兜底、接管模式的蓝本与 `pm grant` / `appops` 授权清单（详见主仓库设计稿 §12）。
  它的机型知识只作核对来源，不作记录源。
- `cmd package query-activities` 的解析按「`priority=N` 行 + 紧随的 `包名/类名` 行」的形状写，容忍大小写与多余空白；
  P1 真机验收（坚果 N1 / 模拟器）时以真实输出校对。
- 已知上游坑：Windows 上 `pack.py --lock` 会把锁文件写成 CRLF，而服务器 `read_lock` 按原字节算哈希、
  装依赖按归一后的文本算哈希，两处对不上会导致依赖永远「没装好」——锁文件保持 LF 即可绕开。

### 验证

```bash
python3 -m unittest discover -s plugins/tv-home/tests -t plugins/tv-home -v
python3 <MovieClaw>/src/movieclaw_agent/builtin-skills/movieclaw-plugin-dev/scripts/check_plugin.py plugins/tv-home
python3 scripts/pack.py plugins/tv-home --output /tmp/tv-home-0.1.0.mcplugin
```

单测用假的 adb 链接（`tests/fake_adb.py`）覆盖：换桌面的七步与回滚、还原顺序、状态记录合并、
安装输出没有 `Success` 就停、`close` 只认「连接被拒绝」。装进真实服务器、按设计稿 §11 的 P1 验收
（模拟器逐条行为 + 真机）在单测之外。跑测试需要 MovieClaw 的源码在 `PYTHONPATH`（CI 已带）。
