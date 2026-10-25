"""电视桌面的设备操作：probe / install / home / verify / restore / close 的业务逻辑。

这一层是同步的纯逻辑：拿一个 ``AdbLink``（或单测里的假链接）干活，返回普通字典。
业务失败（比如校验不过、判为 C 级）用 ``ok=False`` 的结果表达，不抛异常；
连接与命令失败（AdbError）才会抛出去。异步、文件与 HTTP 都在 routes.py。

换桌面的顺序与防护规则沿用 cleanyourtv（MIT）踩出来的做法，规格见主仓库
``docs/design/androidtv-home.md`` §8.4–§8.6。
"""

import re
import tempfile
import time
import os

from .records import merge_record, rescue_text

SHELL_PKG = "io.movieclaw.androidtv.home"
SHELL_ACTIVITY = SHELL_PKG + "/.HomeActivity"
MAIN_PKG = "io.movieclaw.androidtv"

# 桌面壳有两种（设计稿 §6）：默认自带的极简壳；可选的 aerial-tv（MIT，tvOS 风格）。
# shell 名 → (包名, 启动 Activity)。aerial 的 Activity 取自它 install.sh 的 set-home-activity 写法。
SHELL_PKGS = {
    "movieclaw": (SHELL_PKG, SHELL_ACTIVITY),
    "aerial": ("app.orchard.tv", "app.orchard.tv/.MainActivity"),
}

MAIN_ACTION = "android.intent.action.MAIN"
HOME_CATEGORY = "android.intent.category.HOME"

REMOTE_SHELL_APK = "/data/local/tmp/movieclaw-home.apk"
REMOTE_MAIN_APK = "/data/local/tmp/movieclaw-app.apk"

# 「能当家的桌面」之外的占位：设置应用的 FallbackHome、设置向导完成后的组件不算
# （设计稿 §8.4 第 5 步）。设置向导（setupwraith）在真机上可能接管，P1 没有机型记录时一律拒绝。
NON_VIABLE_CLASS_MARKS = ("FallbackHome",)
NON_VIABLE_PKGS = {"com.android.settings"}

COMPONENT_RE = re.compile(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/([A-Za-z0-9_.$]+)")
PRIORITY_RE = re.compile(r"priority=(-?\d+)", re.IGNORECASE)
PROP_RE = re.compile(r"^\[(?P<key>[^\]]+)\]: \[(?P<value>.*)\]$", re.MULTILINE)

# 注入主页键后等界面切过去再读前台；单测把它改成 0
SLEEP_AFTER_HOME_KEY = 1.5


def _full_component(pkg: str, cls: str) -> str:
    return f"{pkg}/{pkg + cls if cls.startswith('.') else cls}"


def parse_props(text: str) -> dict:
    """解析 ``getprop`` 全量输出（``[键]: [值]`` 一行一个）。"""
    return {m.group("key"): m.group("value") for m in PROP_RE.finditer(text)}


def parse_candidates(text: str) -> list:
    """解析 ``cmd package query-activities`` 的输出，兼容两种形状。

    - 老版本：``priority=N`` 一行 + 裸 ``包名/类名`` 一行；
    - API 34 起：``Activity #N:`` 块里 ``priority=N`` + ``ActivityInfo:`` 后跟
      ``name=类`` / ``packageName=包`` / ``enabled=…`` 分行（应用信息块嵌在里面，不配对）。

    组件级停用（enabled=false）的候选照样列出但带标记，换桌面判定时不算数。
    """
    out = []
    priority = None
    cur = None

    def flush():
        nonlocal cur
        if cur and cur.get("pkg") and cur.get("name"):
            out.append({
                "component": _full_component(cur["pkg"], cur["name"]),
                "priority": cur["priority"],
                "enabled": cur["enabled"],
            })
        cur = None

    for raw in text.splitlines():
        line = raw.strip()
        pm = PRIORITY_RE.search(line)
        if pm:
            priority = int(pm.group(1))
            if cur is not None:
                cur["priority"] = priority
        if line.startswith("Activity #"):
            flush()
            continue
        if line.startswith("ActivityInfo:"):
            flush()
            cur = {"priority": priority, "enabled": True, "name": None, "pkg": None, "done": False}
            continue
        if cur is not None:
            if line.startswith("enabled="):
                cur["enabled"] = "enabled=false" not in line
            elif cur["done"]:
                continue
            elif line.startswith("name="):
                value = line[5:].strip()
                if "/" in value:  # 个别固件 name=包/类 一行给全
                    out.append({"component": value, "priority": cur["priority"], "enabled": cur["enabled"]})
                    cur.update(name=None, pkg=None, done=True)
                else:
                    cur["name"] = value or None
            elif line.startswith("packageName="):
                cur["pkg"] = line[12:].strip() or None
                if cur["name"] and cur["pkg"]:
                    out.append({
                        "component": _full_component(cur["pkg"], cur["name"]),
                        "priority": cur["priority"],
                        "enabled": cur["enabled"],
                    })
                    cur.update(name=None, pkg=None, done=True)
        elif "/" in line:
            for pkg, cls in COMPONENT_RE.findall(line):  # 形状 A：裸 包/类
                out.append({"component": _full_component(pkg, cls), "priority": priority, "enabled": True})
                priority = None  # 每个候选的优先级要有据可查，用掉即清
    flush()
    return out


def _candidate_sort_key(c: dict):
    return c["priority"] if c["priority"] is not None else 0


def parse_single_component(text: str) -> "str | None":
    """从 resolve / 角色类输出里取组件。

    形状 B（API 34 起）取 ``ActivityInfo:`` 后第一对 ``name=`` + ``packageName=``；
    形状 A 取最后一个 ``包名/类名``。两样都沾不到返回 None。"""
    name = pkg = None
    found = None
    in_activity = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("ActivityInfo:"):
            in_activity, name, pkg = True, None, None
            continue
        if in_activity:
            if line.startswith("ApplicationInfo:"):
                in_activity = False
            elif line.startswith("name=") and name is None:
                value = line[5:].strip()
                name = value or None
                if "/" in value:
                    found = value
            elif line.startswith("packageName=") and pkg is None:
                pkg = line[12:].strip() or None
                if name and pkg and found is None:
                    return _full_component(pkg, name)
        else:
            for p, c in COMPONENT_RE.findall(line):
                found = _full_component(p, c)
    return found


def parse_version_name(text: str) -> "str | None":
    m = re.search(r"versionName=(\S+)", text)
    return m.group(1) if m else None


def _is_viable_home(candidate: dict) -> bool:
    pkg, _, cls = candidate["component"].partition("/")
    if pkg in NON_VIABLE_PKGS:
        return False
    return not any(mark in cls for mark in NON_VIABLE_CLASS_MARKS)


def resolve_home(link) -> "str | None":
    out = link.shell(f"cmd package resolve-activity -a {MAIN_ACTION} -c {HOME_CATEGORY}")
    return parse_single_component(out)


def home_role(link) -> "str | None":
    """HOME 角色的持有者。Android 10 的 ``cmd role`` 没有 get-role-holders，退回 dumpsys。"""
    out = link.shell("cmd role get-role-holders android.app.role.HOME")
    holder = parse_single_component(out)
    if holder:
        return holder
    dump = link.shell("dumpsys role")
    section = None
    holder = None
    for line in dump.splitlines():
        if "android.app.role.HOME" in line:
            section = True
            continue
        if section and "Role" in line and "android.app.role.HOME" not in line:
            break
        if section:
            for pkg, cls in COMPONENT_RE.findall(line):
                holder = _full_component(pkg, cls)
    return holder


def foreground_pkg(link) -> "str | None":
    """注入主页键后前台是谁（设计稿 §8.4 第 6 步）。

    先看 ResumedActivity / topResumedActivity；某些固件上 dumpsys 只有 mCurrentFocus
    可用（aerial-tv 的做法），作为兜底。"""
    dump = link.shell("dumpsys activity activities")
    for line in dump.splitlines():
        if "ResumedActivity" in line or "topResumedActivity" in line:
            m = COMPONENT_RE.search(line)
            if m:
                return m.group(1)
    for line in dump.splitlines():
        if "mCurrentFocus" in line:
            m = COMPONENT_RE.search(line)
            if m:
                return m.group(1)
    return None


def pm_path(link, pkg: str) -> "str | None":
    out = link.shell(f"pm path {pkg}")
    for line in out.splitlines():
        if line.startswith("package:"):
            return line[len("package:"):].strip()
    return None


def version_of(link, pkg: str) -> "str | None":
    return parse_version_name(link.shell(f"dumpsys package {pkg}"))


def disabled_packages(link) -> list:
    out = link.shell("pm list packages -d")
    return [line[len("package:"):].strip() for line in out.splitlines() if line.startswith("package:")]


def serial_of(props: dict, host: str) -> str:
    serial = (props.get("ro.serialno") or "").strip()
    return serial or host.replace(":", "_")


def snapshot(link, host: str) -> dict:
    """改设备前的快照：原 HOME、相关包的原状态、固件指纹（§8.6）。两种壳都记。"""
    props = parse_props(link.shell("getprop"))
    original = resolve_home(link)
    packages: dict = {}
    if original and original.split("/")[0] not in {p for p, _ in SHELL_PKGS.values()}:
        packages[original.split("/")[0]] = {"was_enabled": True}
    for shell_pkg in {p for p, _ in SHELL_PKGS.values()}:
        packages[shell_pkg] = {"was_installed": pm_path(link, shell_pkg) is not None}
    packages[MAIN_PKG] = {"was_installed": pm_path(link, MAIN_PKG) is not None}
    return {
        "serial": serial_of(props, host),
        "host": host,
        "fingerprint": props.get("ro.build.fingerprint", ""),
        "original_home": original
        if original and original.split("/")[0] not in {p for p, _ in SHELL_PKGS.values()}
        else None,
        "packages": packages,
    }


def probe(link, host: str) -> dict:
    """只读摸底（§8.3 probe）。P1 没有机型记录，级别一律按 B：只换桌面、不去臃肿。"""
    props = parse_props(link.shell("getprop"))
    candidates = parse_candidates(link.shell(
        f"cmd package query-activities -a {MAIN_ACTION} -c {HOME_CATEGORY}"))
    mem = ""
    for line in link.shell("cat /proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem = line
            break
    mem_kb = re.sub(r"\D", "", mem) or None
    return {
        "host": host,
        "model": props.get("ro.product.model", ""),
        "device": props.get("ro.product.device", ""),
        "name": props.get("ro.product.name", ""),
        "android": props.get("ro.build.version.release", ""),
        "sdk": props.get("ro.build.version.sdk", ""),
        "serial": serial_of(props, host),
        "fingerprint": props.get("ro.build.fingerprint", ""),
        "adb_secure": props.get("ro.adb.secure", ""),
        "mem_total_mb": round(int(mem_kb) / 1024) if mem_kb else None,
        "home_resolved": resolve_home(link),
        "home_role": home_role(link),
        "home_candidates": candidates,
        "movieclaw": {"installed": pm_path(link, MAIN_PKG) is not None, "version": version_of(link, MAIN_PKG)},
        "shells": {
            name: {"installed": pm_path(link, pkg) is not None, "version": version_of(link, pkg)}
            for name, (pkg, _) in SHELL_PKGS.items()
        },
        "level": "B",
        "level_note": "这台还没有验证过的机型记录：只换桌面、不停用别的应用、不去臃肿。",
    }


def push_and_install(link, apk_bytes: bytes, remote_path: str) -> str:
    """推 APK → ``pm install -r``。输出没有 Success 就算失败（§8.4 第 2 步）；预编译由调用方做。"""
    if not apk_bytes:
        raise ValueError("APK 内容为空")
    fd, local = tempfile.mkstemp(prefix="tv-home-", suffix=".apk")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(apk_bytes)
        link.push(local, remote_path)
        out = link.shell(f"pm install -r {remote_path}")
    finally:
        try:
            os.remove(local)
        except OSError:
            pass
        link.shell(f"rm -f {remote_path}")
    if "Success" not in out:
        raise RuntimeError(f"pm install 没有返回 Success：\n{out.strip()}")
    return out


def _compile(link, pkg: str, mode: str) -> None:
    out = link.shell(f"cmd package compile -m {mode} -f {pkg}")
    if "Success" not in out:
        raise RuntimeError(f"预编译 {pkg} 没有返回 Success：\n{out.strip()}")


def install_shell(link, apk_bytes: bytes, shell: str = "movieclaw") -> None:
    pkg, _ = SHELL_PKGS[shell]
    push_and_install(link, apk_bytes, REMOTE_SHELL_APK)
    _compile(link, pkg, "speed")


def install_main(link, apk_bytes: bytes) -> None:
    push_and_install(link, apk_bytes, REMOTE_MAIN_APK)
    _compile(link, MAIN_PKG, "speed-profile")


def run_install_shell(link, record: "dict | None") -> dict:
    """只装（或重装）桌面壳，不动桌面。装不上判为 C 级（§8.4 第 2 步）。"""
    snap = snapshot(link, link.host)
    record = merge_record(record, snap)
    return {"record": record, "serial": snap["serial"], "say": "桌面壳已就绪，接着调 home 换桌面。"}


def run_home(link, record: "dict | None", shell_apk_bytes: "bytes | None" = None, shell: str = "movieclaw") -> dict:
    """换桌面（§8.4 的七步）。业务失败返回 ok=False 与救援文本；不改记录里的旧条目。

    shell：装哪种桌面壳。movieclaw（默认）的 APK 随插件包发，字节必给；
    aerial 的 APK 在 install 时下载，这里只要求已安装（shell_apk_bytes 为 None）。
    """
    shell_pkg, shell_activity = SHELL_PKGS[shell]
    shell_name = "「MovieClaw 桌面」" if shell == "movieclaw" else "aerial-tv 桌面"
    snap = snapshot(link, link.host)
    record = merge_record(record, snap)
    serial = snap["serial"]

    def failed(say: str, notes: "list[str] | None" = None) -> dict:
        res = {"ok": False, "say": say, "level": "C", "record": record, "serial": serial,
               "rescue": rescue_text(record, notes or [])}
        return res

    # 第 2 步：装桌面壳；安装就报错的固件判为 C 级
    if shell_apk_bytes is not None:
        try:
            install_shell(link, shell_apk_bytes, shell)
        except (RuntimeError, ValueError) as e:
            return failed(f"桌面壳装不上，电视没动：{e}\n这台多半属于「拒装声明 HOME 的包」的固件（C 级）。")
    elif pm_path(link, shell_pkg) is None:
        return failed(f"{shell_name}还没装：先调 install（shell={shell}）再 home。")

    # 第 3 步：单独启动，证明桌面壳真能起来；随后停掉这份普通任务（否则成为桌面后起第二份）
    out = link.shell(f"am start -W -n {shell_activity}")
    if "Status: ok" not in out:
        return failed(f"桌面壳单独启动失败（am start -W 没有返回 Status: ok）：\n{out.strip()}")
    link.shell(f"am force-stop {shell_pkg}")

    # 第 4 步：设默认桌面（--user 0 照 aerial-tv 的写法，显式钉住用户）
    link.shell(f"cmd package set-home-activity --user 0 {shell_activity}")
    resolved = resolve_home(link)
    disabled_pkg = None

    if resolved is None or resolved.split("/")[0] != shell_pkg:
        # 第 5 步：没生效，看是不是厂商桌面优先级更高、需要停用
        candidates = parse_candidates(link.shell(
            f"cmd package query-activities -a {MAIN_ACTION} -c {HOME_CATEGORY}"))
        others = [c for c in candidates
                  if c["component"].split("/")[0] != shell_pkg and _is_viable_home(c) and c.get("enabled", True)]
        others.sort(key=_candidate_sort_key, reverse=True)
        if not others:
            return failed(f"设默认桌面没生效，又找不到能停用的厂商桌面（HOME 候选：{candidates}）。"
                          f"不动设备，判为 C 级。")
        top = others[0]
        tied = [c for c in others if _candidate_sort_key(c) == _candidate_sort_key(top)]
        if len(tied) > 1:
            return failed(f"有两个同样高的 HOME 候选分不清谁当家（{tied}），不敢停用。判为 C 级。")
        if _candidate_sort_key(top) <= 0:
            return failed(f"厂商桌面 {top['component']} 的优先级是 {_candidate_sort_key(top)}，"
                          f"设默认本应生效却没生效。不动设备，判为 C 级。")
        blockers = [c for c in others[1:]
                    if _candidate_sort_key(c) >= 0 or c["priority"] is None]
        if blockers:
            return failed(
                f"停用 {top['component']} 之后，这些候选的优先级仍不低于桌面壳、可能抢当 HOME："
                f"{[c['component'] for c in blockers]}。真机 Google TV 的设置向导就属于这种情况，"
                f"要有机型记录才能继续。判为 C 级。")
        link.shell(f"pm disable-user --user 0 {top['component'].split('/')[0]}")
        disabled_pkg = top["component"]
        resolved = resolve_home(link)
        if resolved is None or resolved.split("/")[0] != shell_pkg:
            link.shell(f"pm enable {disabled_pkg.split('/')[0]}")
            return failed(f"停用厂商桌面后 HOME 仍没到桌面壳，已重新启用 {disabled_pkg}。判为 C 级。")

    # 第 6 步：注入一次主页键，前台应该是桌面壳（movieclaw 壳会再拉起 MovieClaw 主 App）
    link.shell("input keyevent KEYCODE_HOME")
    time.sleep(SLEEP_AFTER_HOME_KEY)
    fg = foreground_pkg(link)
    accepted = {shell_pkg} | ({MAIN_PKG} if shell == "movieclaw" else set())
    if fg not in accepted:
        notes = []
        if disabled_pkg:
            link.shell(f"pm enable {disabled_pkg.split('/')[0]}")
            notes.append(f"已重新启用 {disabled_pkg}；如需彻底还原再执行 restore。")
        original = record.get("original_home")
        if original:
            link.shell(f"cmd package set-home-activity --user 0 {original}")
            notes.append(f"已尝试把默认桌面设回 {original}。")
        return failed(f"注入主页键后前台是 {fg or '未知'}，不是{shell_name}，已回滚。"
                      f"请再按一下真遥控器的主页键确认；真遥控器也回不去的话，这台的主页键被厂商固定了（C 级）。",
                      notes)

    say = (f"换桌面完成：主页键现在落到{shell_name}"
           f"{'，厂商桌面 ' + disabled_pkg + ' 已停用（记录在案，可还原）' if disabled_pkg else '，厂商桌面保持启用'}。"
           f"请按一下真遥控器的主页键，告诉我屏幕上是什么；结束时我会调 close 帮你关掉调试。")
    return {"ok": True, "say": say, "level": "B", "record": record, "serial": serial,
            "disabled": disabled_pkg, "foreground": fg, "rescue": None}


def run_verify(link, record: "dict | None") -> dict:
    """汇总现状（§8.3 verify）：HOME 归属、角色、被停用的包与记录是否一致。"""
    resolved = resolve_home(link)
    candidates = parse_candidates(link.shell(
        f"cmd package query-activities -a {MAIN_ACTION} -c {HOME_CATEGORY}"))
    disabled = set(disabled_packages(link))
    shell_pkgs = {p for p, _ in SHELL_PKGS.values()}
    recorded = {pkg for pkg, st in (record or {}).get("packages", {}).items()
                if st.get("was_enabled") and pkg not in shell_pkgs}
    unexpectedly = sorted(pkg for pkg in recorded if pkg not in disabled)
    missing = sorted(pkg for pkg in recorded if pkg in disabled)
    ok = resolved is not None and resolved.split("/")[0] in shell_pkgs
    say = (f"HOME 现在解析到 {resolved or '未知'}。"
           + (f"记录里该停用的包都在停用状态。" if recorded and not unexpectedly
              else f"注意：{unexpectedly} 在记录里是停用前就启用的包，现在被停用了。" if unexpectedly
              else "")
           + (f"以下包按记录处于停用：{missing}。" if missing else ""))
    return {"ok": ok, "say": say, "home": resolved, "role": home_role(link),
            "candidates": candidates, "disabled": sorted(disabled),
            "unexpectedly_disabled": unexpectedly}


def run_restore(link, record: "dict | None", uninstall_main: bool = False) -> dict:
    """还原（§8.6）：先启用厂商桌面、设回原 HOME，再卸桌面壳。顺序不能反。

    两种壳都可能卸：记录里「我们装上的」（was_installed=False）才卸，
    用户原本就有的壳保留。"""
    if not record:
        return {"ok": False,
                "say": "这台电视没有状态记录，不知道改过什么，拒绝盲操作。"
                       "厂商桌面没被停用的话，可以直接在系统设置里卸载桌面壳；"
                       "被停用的应用要在系统设置的应用列表里逐个启用。"}
    steps = []
    shell_pkgs = {p for p, _ in SHELL_PKGS.values()}
    disabled_now = set(disabled_packages(link))
    for pkg, st in record.get("packages", {}).items():
        if pkg in shell_pkgs:
            continue
        if st.get("was_enabled") and pkg in disabled_now:
            link.shell(f"pm enable {pkg}")
            steps.append(f"已重新启用 {pkg}")
    original = record.get("original_home")
    if original:
        link.shell(f"cmd package set-home-activity --user 0 {original}")
        steps.append(f"已把默认桌面设回 {original}")
    for shell_pkg in shell_pkgs:
        if (record.get("packages", {}).get(shell_pkg, {}).get("was_installed") is False
                and pm_path(link, shell_pkg) is not None):
            out = link.shell(f"pm uninstall {shell_pkg}")
            steps.append(f"已卸载桌面壳 {shell_pkg}（{out.strip().splitlines()[-1] if out.strip() else 'ok'}）")
        elif record.get("packages", {}).get(shell_pkg, {}).get("was_installed"):
            steps.append(f"{shell_pkg} 是你原本就装的，保留")
    if uninstall_main and pm_path(link, MAIN_PKG) is not None:
        link.shell(f"pm uninstall {MAIN_PKG}")
        steps.append(f"已按要求卸载 {MAIN_PKG}")
    resolved = resolve_home(link)
    expect = original.split("/")[0] if original else None
    ok = expect is None or (resolved is not None and resolved.split("/")[0] == expect)
    say = "；".join(steps) + f"。HOME 现在解析到 {resolved or '未知'}。"
    if not ok:
        say += "（和记录里的原桌面不一致，请在系统设置里再确认一次默认桌面）"
    return {"ok": ok, "say": say, "home": resolved}


def check_port_closed(host: str, port: int = 5555, timeout_s: float = 5.0) -> bool:
    """只认「连接被拒绝」为已关（§8.3 close）；超时或连得上都算没关。"""
    import socket

    s = socket.socket()
    s.settimeout(timeout_s)
    try:
        s.connect((host, port))
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    finally:
        s.close()
    return False
