#!/usr/bin/env python3
"""Copy bundled MovieClaw plugins into example/ as reference sources.

The MovieClaw checkout is only read. Nothing is written back to it.
Copies land under example/<plugin id>/, next to example/hello-note, and are
not placed in plugins/, so they are not indexed or released.

    python3 scripts/sync_examples.py <commit|tag|branch>
    python3 scripts/sync_examples.py --source /path/to/MovieClaw

Tests are copied only when ``python -m unittest discover`` collects and
passes them without the MovieClaw host. Otherwise they are omitted and the
plugin README says why. Each README records the upstream path, commit, and
runtime. example/UPSTREAM.json is the machine-readable commit record.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from pluginlib import ID_RE

ROOT = Path(__file__).resolve().parents[1]
MOVIECLAW_GIT = "https://github.com/movieclaw/MovieClaw.git"
MOVIECLAW_WEB = "https://github.com/movieclaw/MovieClaw"
RECORD_NAME = "UPSTREAM.json"
MARK_BEGIN = "<!-- sync-examples:begin -->"
MARK_END = "<!-- sync-examples:end -->"
PROTECTED_IDS = {"hello-note"}
SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
# Importing any of these means the test is not standalone.
HOST_ROOTS = {
    "movieclaw_api",
    "movieclaw_db",
    "movieclaw_playback",
    "movieclaw_kernel",
    "movieclaw_downloader",
    "movieclaw_channel",
    "movieclaw_plugins",
    "movieclaw_agent",
    "movieclaw_enrich",
    "movieclaw_llm",
    "pytest",
}
ORDER = [
    "watch-presence",
    "weixin-channel",
    "telegram-channel",
    "discord-channel",
    "feishu-channel",
    "qbittorrent-downloader",
    "transmission-downloader",
    "strm-prefetch",
]
DEMOS = {
    "watch-presence": "进程内观看动态：成员区路由和播放记录，直接用宿主内部模块",
    "weixin-channel": "向 im-channels 贡献微信通道（扫码绑定、iLink、入站图片）",
    "telegram-channel": "向 im-channels 贡献 Telegram bot（私聊对话和推送）",
    "discord-channel": "向 im-channels 贡献 Discord bot（Gateway）",
    "feishu-channel": "向 im-channels 贡献飞书群机器人（只推送，不能对话）",
    "qbittorrent-downloader": "向 downloader-adapters 贡献 qBittorrent",
    "transmission-downloader": "向 downloader-adapters 贡献 Transmission",
    "strm-prefetch": "用任务和敏感播放事件提前探网盘规格，探测仍走宿主操作",
}
# Used when the in-tree tests cannot run standalone. Keys are plugin ids.
TEST_NOTES = {
    "watch-presence": (
        "上游目录里有 `tests/test_policy.py` 和 `tests/test_presence.py`，都不能单独跑，已省略。"
        "`test_presence.py` 要装进真实 MovieClaw 进程，用 pytest，还要数据库和登录会话。"
        "`test_policy.py` 也用 pytest；一旦导入 `watch_presence`，就会加载宿主内部的 "
        "`movieclaw_api`、`movieclaw_db`、`movieclaw_playback`、`movieclaw_kernel`。"
    ),
    "weixin-channel": (
        "插件目录里没有 `tests/`。用例在 MovieClaw 的 `tests/channel/`（如 `test_weixin_media.py`）"
        "和 `tests/api/`，从 `movieclaw_plugins.weixin` 导入，依赖宿主夹具，不能对着 "
        "`example/weixin-channel` 单独跑，已省略。"
    ),
    "telegram-channel": (
        "插件目录里没有 `tests/`。用例在 MovieClaw 的 `tests/channel/test_im_adapters.py` 和 "
        "`test_im_inbound_images.py`，从 `movieclaw_plugins.telegram` 导入，依赖宿主，已省略。"
    ),
    "discord-channel": (
        "插件目录里没有 `tests/`。用例在 MovieClaw 的 `tests/channel/test_im_adapters.py` 和 "
        "`test_im_inbound_images.py`，从 `movieclaw_plugins.discord` 导入，依赖宿主，已省略。"
    ),
    "feishu-channel": (
        "插件目录里没有 `tests/`。用例在 MovieClaw 的 `tests/channel/test_feishu.py`，从 "
        "`movieclaw_plugins.feishu` 导入，还要数据库夹具，已省略。"
    ),
    "qbittorrent-downloader": (
        "插件目录里没有 `tests/`。用例在 MovieClaw 的 `tests/downloader/unit/test_qbittorrent.py` "
        "和 `tests/downloader/test_state_normalize.py`，从 `movieclaw_plugins.qbittorrent` 导入，"
        "并依赖 `movieclaw_downloader` 和 `qbittorrentapi`，已省略。"
    ),
    "transmission-downloader": (
        "插件目录里没有 `tests/`。用例在 MovieClaw 的 `tests/downloader/unit/test_transmission.py` "
        "和 `tests/downloader/test_state_normalize.py`，从 `movieclaw_plugins.transmission` 导入，"
        "并依赖 `movieclaw_downloader` 和 `transmission_rpc`，已省略。"
    ),
    "strm-prefetch": (
        "插件目录里没有 `tests/`。用例在 MovieClaw 的 `tests/api/test_strm_prefetch_plugin.py`，"
        "要起真实应用并打桩探测，不能对着 `example/strm-prefetch` 单独跑，已省略。"
    ),
}


def run_git(checkout: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def require_clean_sha(checkout: Path) -> str:
    """Return HEAD. Refuse a dirty tree so the recorded sha matches the bytes."""
    if not (checkout / ".git").exists():
        raise SystemExit(f"{checkout} 不是 git 仓库，无法记录提交号")
    dirty = run_git(checkout, "status", "--porcelain").strip()
    if dirty:
        raise SystemExit(
            f"{checkout} 有未提交改动，已停止。"
            "MovieClaw 不会被修改，也不能在脏工作区上记录提交号。"
        )
    sha = run_git(checkout, "rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"无法解析提交号：{sha}")
    return sha


def clone_ref(ref: str, parent: Path) -> Path:
    dest = parent / "MovieClaw"
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(
        ["git", "-C", str(dest), "remote", "add", "origin", MOVIECLAW_GIT],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(dest), "checkout", "--detach", "FETCH_HEAD"],
        check=True,
    )
    return dest


def discover(checkout: Path) -> list[Path]:
    watch = checkout / "plugins" / "watch-presence"
    if not (watch / "movieclaw-plugin.toml").is_file():
        raise SystemExit("上游没有 plugins/watch-presence/movieclaw-plugin.toml")
    bundled = checkout / "src" / "movieclaw_plugins"
    if not bundled.is_dir():
        raise SystemExit("上游没有 src/movieclaw_plugins")
    manifests = sorted(path for path in bundled.glob("*/movieclaw-plugin.toml") if path.is_file())
    if not manifests:
        raise SystemExit("src/movieclaw_plugins 下没有插件")
    return [watch, *[path.parent for path in manifests]]


def read_manifest(directory: Path) -> dict[str, str]:
    path = directory / "movieclaw-plugin.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"{path} 不是合法的 TOML：{exc}") from exc
    plugin = data.get("plugin")
    if not isinstance(plugin, dict) or not isinstance(plugin.get("id"), str):
        raise SystemExit(f"{path} 缺少 [plugin].id")
    plugin_id = plugin["id"]
    if not ID_RE.fullmatch(plugin_id):
        raise SystemExit(f"{path} 的 id「{plugin_id}」不能作为 example/ 目录名")
    if plugin_id in PROTECTED_IDS:
        raise SystemExit(f"拒绝覆盖 example/{plugin_id}")
    runtime = plugin.get("runtime") or "process"
    if not isinstance(runtime, str):
        raise SystemExit(f"{path} 的 runtime 不是字符串")
    title = plugin.get("title") or plugin_id
    version = plugin.get("version") or ""
    entry = plugin.get("entry") or ""
    return {
        "id": plugin_id,
        "title": str(title),
        "version": str(version),
        "runtime": runtime,
        "entry": str(entry),
    }


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def classify_tests(src: Path) -> tuple[bool, str]:
    """Return (include, note). Include only a tests/ tree unittest can run."""
    tests = src / "tests"
    files = [
        path
        for path in (tests.rglob("*.py") if tests.is_dir() else [])
        if "__pycache__" not in path.parts
    ]
    if not files:
        return False, (
            "插件目录里没有 `tests/`。MovieClaw 仓库 `tests/` 里的相关用例按宿主包路径导入"
            "（例如 `movieclaw_plugins.*`），依赖宿主夹具，不能对着这份副本单独跑，已省略。"
        )
    problems: list[str] = []
    for path in files:
        rel = path.relative_to(src).as_posix()
        try:
            roots = imported_roots(path)
        except SyntaxError as exc:
            problems.append(f"`{rel}` 无法解析（{exc.msg}）")
            continue
        hit = sorted(roots & HOST_ROOTS)
        if hit:
            problems.append(f"`{rel}` 引用了 {'、'.join(hit)}")
    if problems:
        return False, "这些测试不能脱离 MovieClaw 单独跑，已省略：" + "；".join(problems) + "。"
    # Run against a throwaway copy so the MovieClaw checkout stays unmodified.
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="sync-tests-") as tmp:
            work = Path(tmp) / "plugin"
            shutil.copytree(
                src,
                work,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                symlinks=False,
            )
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
    except subprocess.TimeoutExpired:
        return False, "上游 `tests/` 在 60 秒内没有跑完，已省略。"
    blob = f"{proc.stdout}\n{proc.stderr}"
    match = re.search(r"Ran (\d+) test", blob)
    count = int(match.group(1)) if match else 0
    if proc.returncode != 0 or count == 0:
        tail = " / ".join(line.strip() for line in blob.strip().splitlines()[-4:] if line.strip())
        return False, f"上游 `tests/` 不能用 unittest 单独跑通（{tail or '没有用例'}），已省略。"
    return True, f"上游 `tests/` 能用 unittest 单独跑（{count} 个），已原样复制。"


def caveats(meta: dict[str, str], imports: set[str]) -> str:
    paragraphs: list[str] = []
    if meta["id"] == "watch-presence" or meta["runtime"] == "inline":
        paragraphs.append(
            "这份插件是 `runtime = inline`，在主进程里导入，和主程序同一套权限。"
            "它直接 import 宿主内部模块 `movieclaw_api`、`movieclaw_db`、`movieclaw_playback`、"
            "`movieclaw_kernel`，不是只依赖 SDK 的写法。第三方插件不要照这个依赖面抄。"
            "目录里的 `settings.html` 宿主不加载。"
        )
    if meta["id"] in {"qbittorrent-downloader", "transmission-downloader"} or (
        {"qbittorrentapi", "transmission_rpc"} & imports
    ):
        paragraphs.append(
            "下载器依赖宿主已经提供的协议库，不放进插件自己的 `vendor/`："
            "qBittorrent 用 `qbittorrent-api`（import 名 `qbittorrentapi`），"
            "Transmission 用 `transmission-rpc`（import 名 `transmission_rpc`）。"
            "协议库不在运行环境里时，入口模块导入会失败。"
        )
    if meta["runtime"] == "process":
        paragraphs.append(
            "清单是 `runtime = process`。随应用携带时，宿主把它当内置插件装进主进程；"
            "同 id 的插件包装上后会顶掉这一份，卸掉之后随应用的版本回来。"
        )
    if not paragraphs:
        paragraphs.append("对照上游清单和入口模块。不要把这个目录放进 `plugins/`。")
    return "\n\n".join(paragraphs)


def render_readme(
    meta: dict[str, str],
    sha: str,
    *,
    imports: set[str],
    kept_upstream_readme: bool,
) -> str:
    lines = [
        f"# {meta['title']}（参考副本）",
        "",
        "这是从 [MovieClaw](" + MOVIECLAW_WEB + ") 复制过来的参考源码，不是本仓库要发布的插件。",
        "",
        f"- 上游路径：`{meta['upstream']}`",
        f"- 上游提交：`{sha}`",
        "- 权威副本在 MovieClaw 仓库，并随应用一起打包。要改行为，改上游；这里只供对照。",
        f"- 运行方式：`runtime = \"{meta['runtime']}\"`",
        "- 不要放进 `plugins/`，不会进索引，也不要打 Release。",
        f"- 重新同步：`python3 scripts/sync_examples.py {sha}`",
        "",
        "## 注意",
        "",
        caveats(meta, imports),
    ]
    if kept_upstream_readme:
        lines += ["", "上游目录自带的 `README.md` 原样另存为 `README.upstream.md`。"]
    lines += ["", "## 测试", "", meta["tests_note"], ""]
    return "\n".join(lines) + "\n"


def source_imports(src: Path) -> set[str]:
    roots: set[str] = set()
    for path in src.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.relative_to(src).parts:
            continue
        try:
            roots |= imported_roots(path)
        except SyntaxError:
            continue
    return roots


def copy_tree(src: Path, dest: Path, *, include_tests: bool) -> bool:
    """Copy plugin files. Return whether an upstream README was preserved."""
    kept_readme = False
    if dest.exists():
        shutil.rmtree(dest)
    for root, dirs, names in os.walk(src):
        root_path = Path(root)
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in SKIP_DIRS and (include_tests or name != "tests")
        )
        rel_root = root_path.relative_to(src)
        for name in sorted(names):
            path = root_path / name
            if path.is_symlink():
                raise SystemExit(f"上游有软链接，已停止，MovieClaw 未改动：{path}")
            if name.endswith(".pyc") or name == ".DS_Store":
                continue
            rel = (rel_root / name).as_posix()
            target_rel = rel
            if rel == "README.md":
                target_rel = "README.upstream.md"
                kept_readme = True
            target = dest / target_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return kept_readme


def place_plugin(src: Path, dest: Path, meta: dict[str, str], sha: str, include_tests: bool) -> None:
    """Replace dest only after the new tree is fully staged."""
    imports = source_imports(src)
    with tempfile.TemporaryDirectory(prefix="sync-example-") as tmp:
        staging = Path(tmp) / meta["id"]
        kept = copy_tree(src, staging, include_tests=include_tests)
        readme = render_readme(meta, sha, imports=imports, kept_upstream_readme=kept)
        (staging / "README.md").write_text(readme, encoding="utf-8")
        backup: Path | None = None
        if dest.exists():
            backup = dest.with_name(f".{dest.name}.sync-backup")
            if backup.exists():
                shutil.rmtree(backup)
            dest.rename(backup)
        try:
            shutil.copytree(staging, dest)
        except Exception:
            if dest.exists():
                shutil.rmtree(dest)
            if backup is not None and backup.exists():
                backup.rename(dest)
            raise
        if backup is not None and backup.exists():
            shutil.rmtree(backup)


def sort_key(meta: dict[str, str]) -> tuple[int, str]:
    try:
        rank = ORDER.index(meta["id"])
    except ValueError:
        rank = len(ORDER)
    return rank, meta["id"]


def render_listing(plugins: list[dict[str, str]], sha: str, *, dir_prefix: str, record_href: str) -> str:
    lines = [
        f"当前副本的 MovieClaw 提交是 `{sha}`。机器可读的记录在 [`{record_href}`]({record_href})。",
        "权威源码在 [MovieClaw](" + MOVIECLAW_WEB + ")，并随应用打包。不要改这里的副本来发版。要更新：",
        "",
        "```bash",
        f"python3 scripts/sync_examples.py {sha}",
        "```",
        "",
        "| 目录 | 上游路径 | runtime | 示范 |",
        "|---|---|---|---|",
    ]
    for meta in plugins:
        href = f"{dir_prefix}{meta['id']}/"
        lines.append(
            f"| [`{meta['id']}`]({href}) | `{meta['upstream']}` | `{meta['runtime']}` | {meta['demo']} |"
        )
    lines += [
        "",
        "`watch-presence` 是 `runtime = inline`，import 宿主内部的 `movieclaw_api`、`movieclaw_db`、"
        "`movieclaw_playback`、`movieclaw_kernel`，不是只依赖 SDK。"
        "下载器依赖宿主提供的 `qbittorrent-api` / `transmission-rpc`，不放进 `vendor/`。",
        "",
        tests_listing_sentence(plugins),
    ]
    return "\n".join(lines)


def tests_listing_sentence(plugins: list[dict[str, str]]) -> str:
    included = [meta["id"] for meta in plugins if meta.get("tests_included") is True]
    if not included:
        return (
            "这些目录里的测试不能脱离 MovieClaw 单独跑，同步时不复制。"
            "各目录的 `README.md` 写了上游路径、提交号和省略原因。"
        )
    names = "、".join(f"`{plugin_id}`" for plugin_id in included)
    return (
        f"{names} 的 `tests/` 能单独跑，已复制。其余插件的测试依赖 MovieClaw 宿主，已省略。"
        "各目录的 `README.md` 写了上游路径、提交号和原因。"
    )


def replace_marked(path: Path, body: str) -> None:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if MARK_BEGIN not in text or MARK_END not in text:
        print(
            f"warning: {path} 没有同步标记，提交号写在 example/{RECORD_NAME} 和各插件 README.md",
            file=sys.stderr,
        )
        return
    pre, rest = text.split(MARK_BEGIN, 1)
    _, post = rest.split(MARK_END, 1)
    if not pre.endswith("\n"):
        pre += "\n"
    if not post.startswith("\n"):
        post = "\n" + post
    path.write_text(f"{pre}{MARK_BEGIN}\n{body.rstrip()}\n{MARK_END}{post}", encoding="utf-8")


def load_record(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} 不是对象")
    return data


def public_plugin(meta: dict[str, str]) -> dict[str, object]:
    return {
        "id": meta["id"],
        "title": meta["title"],
        "version": meta["version"],
        "runtime": meta["runtime"],
        "upstream": meta["upstream"],
        "demo": meta["demo"],
        "tests_included": meta["tests_included"],
        "tests_note": meta["tests_note"],
    }


def remove_stale(example: Path, previous: dict, keep: set[str]) -> None:
    items = previous.get("plugins") or []
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        plugin_id = item.get("id")
        if not isinstance(plugin_id, str):
            continue
        if plugin_id in keep or plugin_id in PROTECTED_IDS or not ID_RE.fullmatch(plugin_id):
            continue
        target = example / plugin_id
        if target.is_dir():
            shutil.rmtree(target)


def sync_from_checkout(checkout: Path, repo_root: Path, sha: str) -> dict:
    prepared: list[dict[str, str]] = []
    sources: list[tuple[Path, dict[str, str], bool]] = []
    seen: set[str] = set()
    for src in discover(checkout):
        meta = read_manifest(src)
        if meta["id"] in seen:
            raise SystemExit(f"上游插件 id 重复：{meta['id']}")
        seen.add(meta["id"])
        meta["upstream"] = src.relative_to(checkout).as_posix()
        include, note = classify_tests(src)
        if not include and meta["id"] in TEST_NOTES:
            note = TEST_NOTES[meta["id"]]
        meta["tests_included"] = include  # type: ignore[assignment]
        meta["tests_note"] = note
        meta["demo"] = DEMOS.get(
            meta["id"],
            f"随应用携带的插件（上游 `{meta['upstream']}`，runtime = {meta['runtime']}）。",
        )
        sources.append((src, meta, include))
        prepared.append(meta)
    prepared.sort(key=sort_key)

    example = repo_root / "example"
    example.mkdir(parents=True, exist_ok=True)
    record_path = example / RECORD_NAME
    previous = load_record(record_path)
    by_id = {meta["id"]: (src, meta, include) for src, meta, include in sources}
    for meta in prepared:
        src, full, include = by_id[meta["id"]]
        place_plugin(src, example / meta["id"], full, sha, include)
    remove_stale(example, previous, set(by_id))

    record = {
        "repo": MOVIECLAW_WEB,
        "commit": sha,
        "plugins": [public_plugin(meta) for meta in prepared],
    }
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    listing_readme = render_listing(
        prepared,
        sha,
        dir_prefix="example/",
        record_href=f"example/{RECORD_NAME}",
    )
    listing_guide = render_listing(
        prepared,
        sha,
        dir_prefix="",
        record_href=RECORD_NAME,
    )
    replace_marked(repo_root / "README.md", listing_readme)
    replace_marked(repo_root / "example" / "GUIDE.md", listing_guide)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 MovieClaw 的随带插件复制到 example/ 作参考")
    parser.add_argument("ref", nargs="?", help="MovieClaw 的提交、tag 或分支")
    parser.add_argument("--source", type=Path, help="已经检出的 MovieClaw。只读，不会改它")
    parser.add_argument("--repo-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.ref is None) == (args.source is None):
        parser.error("提供一个 MovieClaw ref，或提供 --source，不要两个都给")
    repo_root = args.repo_root.resolve()
    if args.source is not None:
        checkout = args.source.resolve()
        sha = require_clean_sha(checkout)
        record = sync_from_checkout(checkout, repo_root, sha)
    else:
        parent = Path(tempfile.mkdtemp(prefix="movieclaw-sync-"))
        try:
            checkout = clone_ref(args.ref, parent)
            sha = require_clean_sha(checkout)
            record = sync_from_checkout(checkout, repo_root, sha)
        finally:
            shutil.rmtree(parent, ignore_errors=True)
    ids = ", ".join(item["id"] for item in record["plugins"])
    print(f"已从 {record['commit']} 复制 {len(record['plugins'])} 个参考插件到 example/：{ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
