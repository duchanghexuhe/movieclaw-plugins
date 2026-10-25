"""状态记录：改设备前写、还原时用；第一次的记录不被覆盖（同 cleanyourtv 的做法）。

记录存插件私有目录的 ``<序列号>.json``，救援命令存 ``<序列号>-rescue.txt``。
这一层只做字典的读写与合并；异步文件 IO 在 routes.py 的 TvStore 里。
"""

import datetime as _dt

SCHEMA = 1


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def new_record(serial: str, host: str, fingerprint: str) -> dict:
    return {
        "schema": SCHEMA,
        "serial": serial,
        "host": host,
        "fingerprint": fingerprint,
        "created_at": _now(),
        "updated_at": _now(),
        "original_home": None,
        "packages": {},
    }


def merge_record(old: dict | None, snapshot: dict) -> dict:
    """把一次改设备前的快照并进记录：旧条目一律不动，只补新增。

    - ``original_home``：第一次记下后不再变（还原的依据）。
    - ``packages``：每个包的 ``was_enabled`` / ``was_installed`` 只记第一次看到的值。
    """
    if not old:
        base = new_record(snapshot.get("serial", ""), snapshot.get("host", ""), snapshot.get("fingerprint", ""))
        base["original_home"] = snapshot.get("original_home")
        base["packages"] = dict(snapshot.get("packages", {}))
        return base

    merged = {
        "schema": old.get("schema", SCHEMA),
        "serial": old.get("serial") or snapshot.get("serial", ""),
        "host": old.get("host") or snapshot.get("host", ""),
        "fingerprint": old.get("fingerprint") or snapshot.get("fingerprint", ""),
        "created_at": old.get("created_at") or _now(),
        "updated_at": _now(),
        "original_home": old.get("original_home") or snapshot.get("original_home"),
        "packages": dict(old.get("packages", {})),
    }
    for pkg, state in snapshot.get("packages", {}).items():
        if pkg not in merged["packages"]:
            merged["packages"][pkg] = dict(state)
    return merged


def rescue_text(record: dict, notes: list[str]) -> str:
    """把当前能救回原样的命令写下来：连接断了照着手做（设计稿 §8.4 第 6 步）。"""
    lines = [
        "# MovieClaw 电视桌面：救援命令",
        f"# 电视：{record.get('host')}（{record.get('fingerprint', '指纹未知')}）",
        f"# 生成时间：{_now()}",
        "",
    ]
    lines.extend(f"# {note}" for note in notes)
    lines.append("")
    home = record.get("original_home")
    if home:
        lines.append(f"cmd package set-home-activity {home}")
    for pkg, state in record.get("packages", {}).items():
        if pkg == "io.movieclaw.androidtv.home":
            lines.append(f"pm uninstall {pkg}")
        elif state.get("was_enabled"):
            lines.append(f"pm enable {pkg}")
    lines.append("")
    return "\n".join(lines)
