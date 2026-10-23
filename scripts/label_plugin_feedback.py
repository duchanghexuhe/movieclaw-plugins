#!/usr/bin/env python3
"""Labels for an issue opened with the plugin-feedback form.

Prints ``plugin-feedback`` and, when the plugin id is in ``index.json``,
``plugin:<id>``. Prints nothing when the body is not that form. The workflow
creates the labels and applies whatever this prints. GitHub issue forms can
only attach labels that are written into the template, so the per-plugin
label is applied here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from pluginlib import ID_RE

ROOT = Path(__file__).resolve().parents[1]
FEEDBACK_LABEL = "plugin-feedback"
HEADINGS = (
    "### 插件 id",
    "### 插件版本",
    "### MovieClaw 版本",
    "### 描述",
    "### 步骤",
)
_PLUGIN_LINE = re.compile(
    rf"^### 插件 id[ \t]*\n+(?P<id>{ID_RE.pattern[1:-1]})[ \t]*$",
    re.MULTILINE,
)


def _normalize(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n")


def _has_heading(text: str, heading: str) -> bool:
    return re.search(rf"^{re.escape(heading)}[ \t]*$", text, re.MULTILINE) is not None


def feedback_plugin_id(body: str) -> str | None:
    """Plugin id from a plugin-feedback issue body, or None."""
    text = _normalize(body)
    if any(not _has_heading(text, heading) for heading in HEADINGS):
        return None
    match = _PLUGIN_LINE.search(text)
    if match is None:
        return None
    plugin_id = match.group("id")
    return plugin_id if ID_RE.fullmatch(plugin_id) else None


def labels_for(body: str, known: set[str]) -> list[str]:
    """``plugin-feedback``, plus ``plugin:<id>`` when that plugin is in the index."""
    plugin_id = feedback_plugin_id(body)
    if plugin_id is None:
        return []
    labels = [FEEDBACK_LABEL]
    if plugin_id in known:
        labels.append(f"plugin:{plugin_id}")
    return labels


def known_plugin_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return set()
    return {
        item["id"]
        for item in data.get("plugins") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def body_from_event(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    issue = data.get("issue") if isinstance(data, dict) else None
    if not isinstance(issue, dict):
        return ""
    body = issue.get("body")
    return body if isinstance(body, str) else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print labels for a plugin-feedback issue.")
    parser.add_argument(
        "--event",
        type=Path,
        default=os.environ.get("GITHUB_EVENT_PATH"),
        help="GitHub event payload; defaults to GITHUB_EVENT_PATH. Otherwise the body is read from stdin.",
    )
    parser.add_argument("--index", type=Path, default=ROOT / "index.json")
    args = parser.parse_args(argv)
    if args.event is not None:
        body = body_from_event(args.event)
    else:
        body = sys.stdin.read()
    for label in labels_for(body, known_plugin_ids(args.index)):
        print(label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
