"""Pack, tag, and index tests. They do not import MovieClaw."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

import generate_index
import label_plugin_feedback
import pack
import pluginlib
from ci_dirs import lock_files, select

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "example" / "hello-note"


class PackTest(unittest.TestCase):
    def test_example_pack_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.mcplugin"
            second = Path(tmp) / "b.mcplugin"
            digest_a, size_a = pack.pack(EXAMPLE, first)
            digest_b, size_b = pack.pack(EXAMPLE, second)
            self.assertEqual(digest_a, digest_b)
            self.assertEqual(size_a, size_b)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest_a)

    def test_output_inside_the_plugin_is_rejected(self) -> None:
        with self.assertRaises(pluginlib.PluginError):
            pack.pack(EXAMPLE, EXAMPLE / "hello-note-0.1.0.mcplugin")


class TagTest(unittest.TestCase):
    def test_split_last_version_marker(self) -> None:
        self.assertEqual(pluginlib.split_tag("hello-note-v0.1.0"), ("hello-note", "0.1.0"))
        self.assertEqual(
            pluginlib.split_tag("my-cool-plugin-v1.2.3-dev.9"),
            ("my-cool-plugin", "1.2.3-dev.9"),
        )

    def test_rejects_latest_style_tags(self) -> None:
        with self.assertRaises(pluginlib.PluginError):
            pluginlib.split_tag("v0.1.0")
        with self.assertRaises(pluginlib.PluginError):
            pluginlib.split_tag("catalog")

    def test_check_tag_requires_plugins_directory(self) -> None:
        env = os.environ.copy()
        env["TAG"] = "hello-note-v0.1.0"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_tag.py")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("hello-note", proc.stderr)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


MANIFEST = """\
[plugin]
id = "demo-note"
title = "演示"
version = "{version}"
entry = "demo_note"
runtime = "process"
sdk = "^1.0"
description = "一条说明"

[requires]
"downloader-adapters" = "^1.0"

[permissions]
operations = []
network = {network}
"""


class Repo:
    """A throwaway plugins repo with one plugin, released by tagging."""

    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)
        self.plugin = self.root / "plugins" / "demo-note"
        self.plugin.mkdir(parents=True)
        _git(self.root, "init", "-q")
        self.store({})
        (self.plugin / "README.md").write_text("# 演示\n", encoding="utf-8")
        self.notes: dict[str, str] = {}

    def store(self, extra: dict) -> None:
        data = {
            "author": "MovieClaw",
            "license": "MIT",
            "origin": "official",
            "summary": "一句话",
            **extra,
        }
        (self.plugin / "store.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def release(self, version: str, *, network: bool = False, note: str = "- 改了点东西") -> dict:
        (self.plugin / "movieclaw-plugin.toml").write_text(
            MANIFEST.format(version=version, network="true" if network else "false"),
            encoding="utf-8",
        )
        self.notes[version] = note
        body = "".join(f"## {v}\n\n{n}\n\n" for v, n in reversed(self.notes.items()))
        (self.plugin / "CHANGELOG.md").write_text("# 演示\n\n" + body, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", version)
        _git(self.root, "tag", f"demo-note-v{version}")
        return {
            "id": "demo-note",
            "version": version,
            "sha256": hashlib.sha256(version.encode()).hexdigest(),
            "size": 12,
            "released_at": "2026-10-13T01:02:03Z",
        }

    def build(
        self,
        releases: list[dict],
        previous: dict | None = None,
        git_root: Path | None = None,
        download_counts: dict | None = None,
    ) -> dict:
        return generate_index.build_index(
            plugins_dir=self.root / "plugins",
            repo="movieclaw/movieclaw-plugins",
            generated_at="2026-10-13T00:00:00Z",
            previous=previous,
            releases=releases,
            signed=False,
            key_id=None,
            git_root=git_root or self.root,
            download_counts=download_counts,
        )


def _valid(document: dict, tmp: str) -> None:
    out = Path(tmp) / "index.json"
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    generate_index.check_index(out)


class IndexTest(unittest.TestCase):
    def test_tree_reproduces_committed_index(self) -> None:
        # The committed index carries the published versions; regenerating it
        # from the plugin tree and the tags with itself as --previous must give it back.
        self.assertEqual(
            generate_index.main(["--check", str(ROOT / "index.json")]), 0
        )

    def test_release_record_fills_download_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.2.0")
            document = repo.build([record])
            plugin = document["plugins"][0]
            self.assertEqual(document["schema"], 2)
            self.assertEqual(plugin["latest"], "1.2.0")
            self.assertEqual(plugin["latest_stable"], "1.2.0")
            self.assertEqual(plugin["summary"], "一句话")
            self.assertEqual(plugin["category"], "downloader")
            self.assertEqual(plugin["author"], {"name": "MovieClaw", "url": None})
            version = plugin["versions"][0]
            self.assertEqual(version["notes"], "- 改了点东西\n")
            self.assertEqual(version["requires"], {"downloader-adapters": "^1.0"})
            self.assertFalse(version["prerelease"])
            self.assertEqual(
                version["readme"]["url"],
                "https://raw.githubusercontent.com/movieclaw/movieclaw-plugins/"
                "demo-note-v1.2.0/plugins/demo-note/README.md",
            )
            artifact = version["artifacts"][0]
            self.assertEqual(artifact["sha256"], record["sha256"])
            self.assertEqual(artifact["platform"], "any")
            self.assertEqual(
                artifact["url"],
                "https://github.com/movieclaw/movieclaw-plugins/releases/download/"
                "demo-note-v1.2.0/demo-note-1.2.0.mcplugin",
            )
            _valid(document, tmp)

    def test_old_version_keeps_what_it_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            first = repo.release("1.0.0", network=False, note="- 第一版")
            second = repo.release("1.1.0", network=True, note="- 联网了")
            versions = repo.build([first, second])["plugins"][0]["versions"]
            self.assertEqual([v["version"] for v in versions], ["1.1.0", "1.0.0"])
            self.assertTrue(versions[0]["permissions"]["network"])
            self.assertFalse(versions[1]["permissions"]["network"])
            self.assertEqual(versions[1]["notes"], "- 第一版\n")

    def test_tag_without_release_record_is_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            first = repo.release("1.0.0")
            repo.release("1.1.0")
            versions = repo.build([first])["plugins"][0]["versions"]
            self.assertEqual([v["version"] for v in versions], ["1.0.0"])

    def test_shallow_clone_falls_back_to_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty:
            repo = Repo(tmp)
            full = repo.build([repo.release("1.0.0")])
            again = repo.build([], previous=full, git_root=Path(empty))
            self.assertEqual(again, full)

    def test_schema_1_previous_without_tags_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty:
            repo = Repo(tmp)
            repo.release("1.0.0")
            previous = {
                "schema": 1,
                "plugins": [
                    {
                        "id": "demo-note",
                        "versions": [
                            {
                                "version": "1.0.0",
                                "released_at": "2026-10-13T01:02:03Z",
                                "artifacts": [{"platform": "any", "sha256": "a" * 64, "size": 1}],
                            }
                        ],
                    }
                ],
            }
            with self.assertRaisesRegex(pluginlib.PluginError, "git fetch --tags"):
                repo.build([], previous=previous, git_root=Path(empty))
            upgraded = repo.build([], previous=previous)
            self.assertEqual(upgraded["plugins"][0]["versions"][0]["artifacts"][0]["size"], 1)

    def test_yanked_and_prerelease_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            records = [
                repo.release("1.0.0"),
                repo.release("1.1.0"),
                repo.release("1.2.0-beta.2"),
                repo.release("1.2.0-beta.10"),
            ]
            repo.store({"yanked": {"1.1.0": "会删掉做种任务"}})
            plugin = repo.build(records)["plugins"][0]
            self.assertEqual(
                [v["version"] for v in plugin["versions"]],
                ["1.2.0-beta.10", "1.2.0-beta.2", "1.1.0", "1.0.0"],
            )
            self.assertEqual(plugin["latest"], "1.2.0-beta.10")
            self.assertEqual(plugin["latest_stable"], "1.0.0")
            self.assertEqual(plugin["versions"][2]["yanked"], {"reason": "会删掉做种任务"})
            self.assertTrue(plugin["versions"][0]["prerelease"])
            _valid(repo.build(records), tmp)

    def test_yanking_an_unknown_version_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            repo.store({"yanked": {"9.9.9": "笔误"}})
            with self.assertRaisesRegex(pluginlib.PluginError, "9.9.9"):
                repo.build([record])

    def test_icon_becomes_a_content_named_catalog_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
            (repo.plugin / "icon.svg").write_bytes(svg)
            repo.store({"icon": "icon.svg", "category": "tools"})
            assets: dict[str, bytes] = {}
            document = generate_index.build_index(
                plugins_dir=repo.root / "plugins",
                repo="movieclaw/movieclaw-plugins",
                generated_at="2026-10-13T00:00:00Z",
                previous=None,
                releases=[record],
                signed=False,
                key_id=None,
                git_root=repo.root,
                assets=assets,
            )
            plugin = document["plugins"][0]
            digest = hashlib.sha256(svg).hexdigest()
            name = f"demo-note-icon-{digest[:16]}.svg"
            self.assertEqual(assets, {name: svg})
            self.assertEqual(plugin["icon"]["sha256"], digest)
            self.assertTrue(plugin["icon"]["url"].endswith(f"/releases/download/catalog/{name}"))
            self.assertEqual(plugin["category"], "tools")

    def test_bad_images_are_rejected(self) -> None:
        cases = {
            "script": ("icon.svg", b"<svg><script>alert(1)</script></svg>"),
            "handler": ("icon.svg", b'<svg onload="x()"></svg>'),
            "type": ("icon.gif", b"GIF89a"),
            "url": ("https://example.com/i.svg", None),
        }
        for label, (name, data) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                repo = Repo(tmp)
                record = repo.release("1.0.0")
                if data is not None:
                    (repo.plugin / name).write_bytes(data)
                repo.store({"icon": name})
                with self.assertRaises(pluginlib.PluginError):
                    repo.build([record])

    def test_store_json_rules(self) -> None:
        cases = {
            "long summary": {"summary": "长" * 61},
            "bad category": {"category": "games"},
            "bad homepage": {"homepage": "http://example.com"},
            "bad issues url": {"issues_url": "https://github.com/acme/widgets"},
            "bad deprecated": {"deprecated": {"replaced_by": "x"}},
        }
        for label, extra in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                repo = Repo(tmp)
                repo.store(extra)
                with self.assertRaises(pluginlib.PluginError):
                    pluginlib.load_store(repo.plugin)

    def test_store_json_tolerates_old_fields_and_own_raw_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            (repo.plugin / "web").mkdir()
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>'
            (repo.plugin / "web" / "icon.svg").write_bytes(svg)
            repo.store(
                {
                    "min_app": None,
                    "max_app": None,
                    "app": None,
                    "screenshots": None,
                    "icon": "https://raw.githubusercontent.com/movieclaw/movieclaw-plugins/main/"
                    "plugins/demo-note/web/icon.svg",
                }
            )
            store = pluginlib.load_store(repo.plugin)
            self.assertNotIn("min_app", store)
            self.assertEqual(store["screenshots"], [])
            plugin = repo.build([record])["plugins"][0]
            self.assertEqual(plugin["icon"]["sha256"], hashlib.sha256(svg).hexdigest())
            _valid(repo.build([record]), tmp)

    def test_lint_wants_a_changelog_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            repo.release("1.0.0")
            self.assertEqual(generate_index.lint(repo.root / "plugins"), [])
            manifest = repo.plugin / "movieclaw-plugin.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace("1.0.0", "1.1.0"), encoding="utf-8"
            )
            problems = generate_index.lint(repo.root / "plugins")
            self.assertEqual(len(problems), 1)
            self.assertIn("## 1.1.0", problems[0])

    def test_prerelease_ordering(self) -> None:
        key = pluginlib.version_sort_key
        self.assertGreater(key("1.0.0"), key("1.0.0-dev.1"))
        self.assertGreater(key("1.0.0-beta.10"), key("1.0.0-beta.2"))
        self.assertGreater(key("1.0.0-rc.1"), key("1.0.0-beta.9"))
        self.assertGreater(key("1.0.0-beta"), key("1.0.0-1"))
        self.assertEqual(key("1.0.0+build.5"), key("1.0.0"))


class ChangelogTest(unittest.TestCase):
    def test_section_forms(self) -> None:
        text = "# x\n\n## [1.1.0] - 2026-10-21\n\n- b\n### 细节\n- c\n\n## v1.0.0\n\n- a\n"
        self.assertEqual(pluginlib.changelog_section(text, "1.1.0"), "- b\n### 细节\n- c\n")
        self.assertEqual(pluginlib.changelog_section(text, "1.0.0"), "- a\n")
        self.assertIsNone(pluginlib.changelog_section(text, "1.0"))
        self.assertIsNone(pluginlib.changelog_section(text, "0.9.0"))

    def test_heading_does_not_swallow_the_next_line(self) -> None:
        text = "## 1.0.0\n\n- 第一行，\n  续行\n"
        self.assertEqual(pluginlib.changelog_section(text, "1.0.0"), "- 第一行，\n  续行\n")

    def test_lock_packages(self) -> None:
        text = (
            "# python: 3.12\n"
            "#> lz4-4.4.4-cp312-cp312-manylinux_2_17_x86_64.whl 1 sha256:" + "a" * 64 + "\n"
            "lz4==4.4.4 \\\n    --hash=sha256:" + "a" * 64 + "\n"
            "#> six-1.17.0-py2.py3-none-any.whl 1 sha256:" + "b" * 64 + "\n"
            "Six==1.17.0 \\\n    --hash=sha256:" + "b" * 64 + "\n"
        )
        self.assertEqual(
            pluginlib.lock_packages(text),
            [
                {"name": "lz4", "version": "4.4.4", "native": True},
                {"name": "six", "version": "1.17.0", "native": False},
            ],
        )


class CiDirsTest(unittest.TestCase):
    def test_readme_only_skips_movieclaw(self) -> None:
        run, dirs = select(["README.md"])
        self.assertFalse(run)
        self.assertEqual(dirs, [])

    def test_example_change_validates_the_example(self) -> None:
        run, dirs = select(["example/GUIDE.md"])
        record = json.loads((ROOT / "example" / "UPSTREAM.json").read_text(encoding="utf-8"))
        expected = ["example/hello-note"] + [f"example/{item['id']}" for item in record["plugins"]]
        self.assertTrue(run)
        self.assertEqual(dirs, expected)

    def test_script_change_includes_example(self) -> None:
        run, dirs = select(["scripts/pack.py"])
        self.assertTrue(run)
        self.assertIn("example/hello-note", dirs)
        self.assertIn("example/watch-presence", dirs)
        self.assertIn("example/strm-prefetch", dirs)

    def test_reference_examples_follow_upstream_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            example = root / "example"
            (example / "hello-note").mkdir(parents=True)
            (example / "hello-note" / "movieclaw-plugin.toml").write_text("x", encoding="utf-8")
            (example / "weixin-channel").mkdir()
            (example / "weixin-channel" / "movieclaw-plugin.toml").write_text("x", encoding="utf-8")
            (example / "skip-me").mkdir()
            (example / "skip-me" / "movieclaw-plugin.toml").write_text("x", encoding="utf-8")
            (example / "UPSTREAM.json").write_text(
                json.dumps(
                    {
                        "plugins": [
                            {"id": "weixin-channel"},
                            {"id": "missing"},
                            {"id": "../nope"},
                            {"id": "hello-note"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            run, dirs = select(["example/GUIDE.md"], root)
            self.assertTrue(run)
            self.assertEqual(dirs, ["example/hello-note", "example/weixin-channel"])


class DependencyLockTest(unittest.TestCase):
    def plugin(self, tmp: str, table: str, *, lock: str | None = "requirements.lock") -> Path:
        directory = Path(tmp) / "plugins" / "hello-note"
        shutil.copytree(EXAMPLE, directory)
        manifest = directory / "movieclaw-plugin.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + table, encoding="utf-8")
        if lock is not None:
            (directory / lock).parent.mkdir(parents=True, exist_ok=True)
            (directory / lock).write_text("# python: 3.12\n", encoding="utf-8")
        return directory

    def test_without_dependencies_there_is_no_lock(self) -> None:
        self.assertIsNone(pluginlib.dependency_lock(EXAMPLE))

    def test_lock_file_is_packed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.plugin(tmp, '\n[dependencies]\nlock = "requirements.lock"\n')
            self.assertEqual(
                pluginlib.dependency_lock(directory), (directory / "requirements.lock").resolve()
            )
            output = Path(tmp) / "out.mcplugin"
            pack.pack(directory, output)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("requirements.lock", archive.namelist())
            self.assertEqual(lock_files(["plugins/hello-note"], Path(tmp)), [
                "plugins/hello-note/requirements.lock"
            ])

    def test_rejected_locks(self) -> None:
        cases = {
            "missing": ('\n[dependencies]\nlock = "requirements.lock"\n', None),
            "outside": ('\n[dependencies]\nlock = "../requirements.lock"\n', None),
            "skipped by pack": ('\n[dependencies]\nlock = ".deps/requirements.lock"\n',
                                ".deps/requirements.lock"),
            "not a path": ("\n[dependencies]\nlock = 1\n", "requirements.lock"),
        }
        for label, (table, lock) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                directory = self.plugin(tmp, table, lock=lock)
                with self.assertRaises(pluginlib.PluginError):
                    pluginlib.dependency_lock(directory)
                with self.assertRaises(pluginlib.PluginError):
                    pack.pack(directory, Path(tmp) / "out.mcplugin")

    def test_only_process_plugins_take_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.plugin(tmp, '\n[dependencies]\nlock = "requirements.lock"\n')
            manifest = directory / "movieclaw-plugin.toml"
            text = manifest.read_text(encoding="utf-8").replace(
                'runtime = "process"', 'runtime = "inline"'
            )
            manifest.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(pluginlib.PluginError, "process"):
                pluginlib.dependency_lock(directory)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


_FEEDBACK_BODY = """\
### 插件 id

cloud-115

### 插件版本

0.1.0-beta.2

### MovieClaw 版本

_No response_

### 描述

播放失败

### 步骤

1. 打开影片
2. 看到错误
"""


class FeedbackUrlTest(unittest.TestCase):
    def test_default_form_has_plugin_id_and_no_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.2.0")
            plugin = repo.build([record])["plugins"][0]
            url = plugin["feedback_url"]
            self.assertEqual(
                url,
                "https://github.com/movieclaw/movieclaw-plugins/issues/new"
                "?template=plugin-feedback.yml&plugin=demo-note",
            )
            self.assertEqual(set(_query(url)), {"template", "plugin"})
            self.assertNotIn("downloads", plugin)
            _valid(repo.build([record]), tmp)

    def test_issues_url_wins_over_a_github_homepage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            issues = "https://github.com/acme/widgets/issues/new?template=bug.yml&plugin=demo-note"
            repo.store({"homepage": "https://github.com/acme/widgets", "issues_url": issues})
            plugin = repo.build([record])["plugins"][0]
            self.assertEqual(plugin["feedback_url"], issues)
            self.assertEqual(plugin["homepage"], "https://github.com/acme/widgets")

    def test_github_repo_homepage_points_at_its_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            repo.store({"homepage": "https://github.com/acme/widgets/"})
            plugin = repo.build([record])["plugins"][0]
            self.assertEqual(plugin["feedback_url"], "https://github.com/acme/widgets/issues/new")

    def test_catalog_paths_stay_on_the_prefilled_form(self) -> None:
        cases = {
            "tree": "https://github.com/movieclaw/movieclaw-plugins/tree/main/plugins/demo-note",
            "this repo": "https://github.com/movieclaw/movieclaw-plugins",
            "website": "https://example.com/demo",
        }
        for label, homepage in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                repo = Repo(tmp)
                record = repo.release("1.0.0")
                repo.store({"homepage": homepage})
                plugin = repo.build([record])["plugins"][0]
                self.assertEqual(_query(plugin["feedback_url"])["plugin"], ["demo-note"])
                self.assertNotIn("plugin_version", _query(plugin["feedback_url"]))

    def test_schema_allows_the_new_fields_and_their_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            document = repo.build([record])
            plugin = document["plugins"][0]
            plugin.pop("feedback_url")
            _valid(document, tmp)
            plugin["feedback_url"] = "https://github.com/acme/widgets/issues/new"
            plugin["downloads"] = 3
            _valid(document, tmp)
            plugin["downloads"] = -1
            with self.assertRaises(pluginlib.PluginError):
                _valid(document, tmp)
            plugin["downloads"] = True
            with self.assertRaises(pluginlib.PluginError):
                _valid(document, tmp)


class DownloadsTest(unittest.TestCase):
    def test_sums_mcplugin_assets_for_published_versions_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            records = [repo.release("1.0.0"), repo.release("1.1.0")]
            repo.store({"yanked": {"1.0.0": "旧包"}})
            counts = {
                "demo-note": {"1.0.0": 4, "1.1.0": 6, "9.9.9": 100},
                "other-plugin": {"1.0.0": 50},
            }
            plugin = repo.build(records, download_counts=counts)["plugins"][0]
            self.assertEqual(plugin["downloads"], 10)
            again = repo.build(records, previous={"plugins": [plugin]})
            self.assertEqual(again["plugins"][0]["downloads"], 10)

    def test_fetch_payload_ignores_drafts_sidecars_and_other_tags(self) -> None:
        releases = [
            {
                "tag_name": "demo-note-v1.0.0",
                "assets": [
                    {"name": "demo-note-1.0.0.mcplugin", "download_count": 2},
                    {"name": "demo-note-1.0.0-linux.mcplugin", "download_count": 3},
                    {"name": "demo-note-1.0.0.mcplugin.sha256", "download_count": 40},
                ],
            },
            {
                "tag_name": "demo-note-v1.0.0",
                "draft": True,
                "assets": [{"name": "demo-note-1.0.0.mcplugin", "download_count": 9}],
            },
            {
                "tag_name": "catalog",
                "assets": [{"name": "index.json", "download_count": 100}],
            },
            {
                "tag_name": "other-plugin-v1.0.0",
                "assets": [{"name": "other-plugin-1.0.0.mcplugin", "download_count": 7}],
            },
        ]
        self.assertEqual(
            generate_index.mcplugin_download_counts(releases),
            {"demo-note": {"1.0.0": 5}, "other-plugin": {"1.0.0": 7}},
        )

    def test_missing_release_counts_as_zero_when_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            plugin = repo.build([record], download_counts={})["plugins"][0]
            self.assertEqual(plugin["downloads"], 0)

    def test_bad_previous_downloads_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            previous = repo.build([record])
            previous["plugins"][0]["downloads"] = True
            with self.assertRaisesRegex(pluginlib.PluginError, "downloads"):
                repo.build([], previous=previous)

    def test_pages_follow_github_next_and_stop_at_other_hosts(self) -> None:
        first = "https://api.github.com/repos/movieclaw/movieclaw-plugins/releases?per_page=100"
        second = "https://api.github.com/repos/movieclaw/movieclaw-plugins/releases?per_page=100&page=2"
        pages = {
            first: (
                [{
                    "tag_name": "demo-note-v1.0.0",
                    "assets": [{"name": "demo-note-1.0.0.mcplugin", "download_count": 2}],
                }],
                f'<{second}>; rel="next"',
            ),
            second: (
                [{
                    "tag_name": "demo-note-v1.1.0",
                    "assets": [{"name": "demo-note-1.1.0.mcplugin", "download_count": 3}],
                }],
                None,
            ),
        }

        def get(url, token):
            self.assertEqual(token, "sekret")
            self.assertTrue(url.startswith("https://api.github.com/"))
            return pages[url]

        counts = generate_index.fetch_download_counts(
            "movieclaw/movieclaw-plugins", token="sekret", get=get
        )
        self.assertEqual(counts, {"demo-note": {"1.0.0": 2, "1.1.0": 3}})

        def foreign(url, token):
            return [], '<https://evil.example/next>; rel="next"'

        with self.assertRaisesRegex(pluginlib.PluginError, "别的站点"):
            generate_index.fetch_download_counts("movieclaw/movieclaw-plugins", get=foreign)

    def test_fetch_downloads_is_not_part_of_check(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = generate_index.main(
                ["--check", str(ROOT / "index.json"), "--fetch-downloads"]
            )
        self.assertEqual(code, 1)
        self.assertIn("--fetch-downloads", err.getvalue())


class IssueFormTest(unittest.TestCase):
    def test_form_fields_are_prefillable_text_inputs(self) -> None:
        path = ROOT / ".github" / "ISSUE_TEMPLATE" / generate_index.FEEDBACK_TEMPLATE
        form = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(form["labels"], [label_plugin_feedback.FEEDBACK_LABEL])
        fields = [item for item in form["body"] if item["type"] != "markdown"]
        self.assertEqual(
            [(item["id"], item["type"]) for item in fields],
            [
                ("plugin", "input"),
                ("plugin_version", "input"),
                ("movieclaw_version", "input"),
                ("description", "textarea"),
                ("steps", "textarea"),
            ],
        )
        self.assertTrue(all(item["type"] in {"input", "textarea"} for item in fields))
        labels = [item["attributes"]["label"] for item in fields]
        self.assertEqual(tuple(f"### {label}" for label in labels), label_plugin_feedback.HEADINGS)

    def test_catalog_workflow_refreshes_downloads_without_a_new_group(self) -> None:
        catalog = (ROOT / ".github" / "workflows" / "catalog.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("group: plugin-catalog", catalog)
        self.assertIn("group: plugin-catalog", release)
        self.assertIn("cancel-in-progress: false", catalog)
        self.assertIn("cancel-in-progress: false", release)
        self.assertIn('--fetch-downloads', catalog)
        self.assertIn('--fetch-downloads', release)
        self.assertIn('cron: "17 4 * * *"', catalog)


class PluginFeedbackLabelTest(unittest.TestCase):
    def test_known_plugin_gets_both_labels(self) -> None:
        self.assertEqual(
            label_plugin_feedback.labels_for(_FEEDBACK_BODY, {"cloud-115"}),
            ["plugin-feedback", "plugin:cloud-115"],
        )

    def test_unknown_plugin_keeps_the_shared_label_only(self) -> None:
        body = _FEEDBACK_BODY.replace("cloud-115", "not-in-index")
        self.assertEqual(
            label_plugin_feedback.labels_for(body, {"cloud-115"}),
            ["plugin-feedback"],
        )

    def test_other_issues_are_left_alone(self) -> None:
        self.assertEqual(label_plugin_feedback.labels_for("播放失败\n", {"cloud-115"}), [])
        self.assertIsNone(label_plugin_feedback.feedback_plugin_id(_FEEDBACK_BODY.replace("cloud-115", "Nope")))

    def test_cli_reads_the_event_and_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(
                json.dumps({"issue": {"number": 7, "body": _FEEDBACK_BODY}}),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "label_plugin_feedback.py"),
                    "--event",
                    str(event),
                    "--index",
                    str(ROOT / "index.json"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["plugin-feedback", "plugin:cloud-115"])


if __name__ == "__main__":
    unittest.main()
