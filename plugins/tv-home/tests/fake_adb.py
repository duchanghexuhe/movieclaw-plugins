"""假 adb 链接：按一台「会听话的电视」建模，供单测注入各种机型与故障。"""

SHELL_PKG = "io.movieclaw.androidtv.home"
SHELL_ACTIVITY = SHELL_PKG + "/.HomeActivity"
MAIN_PKG = "io.movieclaw.androidtv"
DANGBEI = "com.dangbei.TVHomeLauncher/com.dangbei.leard.leradlauncher.ui.sony.main.MainActivity"
TVLAUNCHER = "com.google.android.tvlauncher/com.google.android.tvlauncher.HomeActivity"
SETUPWRAITH = "com.google.android.setupwraith/com.google.android.setupwraith.MainActivity"
FALLBACK = "com.android.settings/com.android.settings.fallback.FallbackHome"

PROPS_TEXT = """\
[ro.product.model]: [BRAVIA 4K UR2]
[ro.product.device]: [BRAVIA_UR2_4K]
[ro.product.name]: [BRAVIA_UR2_4K_CN]
[ro.build.version.release]: [10]
[ro.build.version.sdk]: [29]
[ro.serialno]: [fc3b17011402353]
[ro.build.fingerprint]: [Sony/BRAVIA_UR2_4K_CN/BRAVIA_UR2_4K:10/QTG3.200305.006.S97/662301:user/release-keys]
[ro.adb.secure]: [1]
"""

MEMINFO_TEXT = "MemTotal:       2369656 kB\nMemAvailable:     740216 kB\n"


def candidates_text(candidates) -> str:
    """按 query-activities 的形状拼：每个候选一行 priority + 一行组件。"""
    out = ["opPackageName=null:"]
    for c in candidates:
        priority = c.get("priority")
        out.append(f"priority={priority if priority is not None else 0} preferredOrder=0 match=0x108000 "
                   f"specificIndex=0 isDefault=false")
        out.append(f"  {c['component']}")
    return "\n".join(out)


def resolve_text(component: str) -> str:
    return f"priority=0 preferredOrder=0 match=0x108000 specificIndex=0 isDefault=true\n  {component}"


def dumpsys_foreground(pkg: str) -> str:
    return (f"  mResumedActivity: ActivityRecord{{8f2e01 u0 {pkg}/.MainActivity t123}}\n"
            "    ...")


class FakeLink:
    """同步的假 adb 链接：状态驱动（装了什么、停了什么、HOME 是谁），shell 调用全部记账。"""

    def __init__(self, host="192.168.1.50", candidates=(), resolved=None,
                 installed=(), disabled=(), foreground=None, foreground_after_home=None,
                 install_output="Success", am_start_output=None, set_home_effective=True):
        self.host = host
        self.candidates = list(candidates)
        self.resolved = resolved
        self.installed = set(installed)
        self.disabled = set(disabled)
        self.foreground = foreground
        self.foreground_after_home = foreground_after_home
        self.install_output = install_output
        self.am_start_output = am_start_output
        self.set_home_effective = set_home_effective
        self.shell_calls = []
        self.pushed = []  # (本地路径, 设备路径, 字节)

    # -- adb 链接的形状 --
    def push(self, local_path, device_path):
        with open(local_path, "rb") as f:
            self.pushed.append((local_path, device_path, f.read()))

    def close(self):
        pass

    # -- shell --
    def shell(self, command, timeout_s=60.0):
        self.shell_calls.append(command)
        if command.startswith("getprop"):
            return PROPS_TEXT
        if command.startswith("cat /proc/meminfo"):
            return MEMINFO_TEXT
        if command.startswith("cmd package query-activities"):
            return candidates_text(self.candidates)
        if command.startswith("cmd package resolve-activity"):
            return resolve_text(self.resolved) if self.resolved else "No activity found"
        if command.startswith("cmd package set-home-activity"):
            target = command.split()[-1]
            if "/" not in target:
                raise AssertionError("set-home-activity 应该带组件")
            # 优先级更高的厂商桌面在位时，系统不认这个设置（模拟器实测）
            if self.set_home_effective:
                self.resolved = target
            return ""
        if command.startswith("cmd package compile"):
            return "Success"
        if command.startswith("dumpsys role"):
            return f"Role: android.app.role.HOME\n  Holders:\n    {self.resolved or ''}\n"
        if command.startswith("cmd role get-role-holders"):
            return self.resolved or ""
        if command.startswith("dumpsys activity activities"):
            return dumpsys_foreground(self.foreground) if self.foreground else ""
        if command.startswith("pm path "):
            pkg = command.split()[-1]
            return f"package:/data/app/{pkg}/base.apk" if pkg in self.installed else ""
        if command.startswith("pm install -r"):
            remote = command.split()[-1]
            pkg = {"/data/local/tmp/movieclaw-home.apk": SHELL_PKG,
                   "/data/local/tmp/movieclaw-app.apk": MAIN_PKG}.get(remote)
            if pkg:
                self.installed.add(pkg)
            return self.install_output
        if command.startswith("pm disable-user"):
            pkg = command.split()[-1]
            self.disabled.add(pkg)
            if self.resolved and self.resolved.split("/")[0] == pkg:
                # 当家的厂商桌面被停用后，HOME 只剩桌面壳能当
                self.resolved = SHELL_ACTIVITY
            return ""
        if command.startswith("pm enable "):
            self.disabled.discard(command.split()[-1])
            return ""
        if command.startswith("pm list packages -d"):
            return "".join(f"package:{p}\n" for p in sorted(self.disabled))
        if command.startswith("pm uninstall "):
            pkg = command.split()[-1]
            self.installed.discard(pkg)
            return "Success"
        if command.startswith("dumpsys package"):
            pkg = command.split()[-1]
            if pkg in self.installed:
                return f"  versionName=0.5.0  versionCode=50\n  pkg={pkg}\n"
            return "Unable to find package"
        if command.startswith("am start -W"):
            if self.am_start_output is not None:
                return self.am_start_output
            if SHELL_PKG in self.installed:
                return "Status: ok\nLaunchState: COLD\nTotalTime: 671\nWaitTime: 690"
            return "Error: Activity class {io.movieclaw.androidtv.home/.HomeActivity} does not exist."
        if command.startswith("input keyevent"):
            if self.foreground_after_home is not None:
                self.foreground = self.foreground_after_home
            else:
                self.foreground = MAIN_PKG if MAIN_PKG in self.installed else SHELL_PKG
            return ""
        if command.startswith("am force-stop") or command.startswith("rm -f"):
            return ""
        raise AssertionError(f"假链接没有为这条命令准备输出：{command}")

    # -- 断言辅助 --
    def called(self, prefix: str) -> bool:
        return any(c.startswith(prefix) for c in self.shell_calls)

    def calls_of(self, prefix: str) -> list:
        return [c for c in self.shell_calls if c.startswith(prefix)]
