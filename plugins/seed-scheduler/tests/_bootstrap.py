"""让纯函数单测在两种环境里都能跑。

装好了插件运行时的环境（上游 CI）直接导入真实模块；没装 ``pydantic`` / ``fastapi`` /
``movieclaw_*`` 的机器上，用 AST 把纯函数（连同依赖的模块级常量）抽出来，塞进一个桩模块执行。
这样单测只依赖标准库，跑得动就是真的跑过。
"""

import ast
import asyncio
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SOURCES = Path(__file__).resolve().parent.parent


def _real_available() -> bool:
    for name in ("pydantic", "fastapi", "movieclaw_sdk", "movieclaw_api", "movieclaw_scheduler"):
        if name in sys.modules:
            return True
    try:
        import pydantic  # noqa: F401,PLC0415
        import movieclaw_sdk  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001 -- 装不上就走下面的抽取路径
        return False
    return True


if _real_available():
    sys.path.insert(0, str(SOURCES))
else:
    SOURCE = SOURCES / "seed_scheduler.py"

    # 要抽出来的东西：纯函数 + 它们依赖的模块级常量。
    WANTED_FUNCS = {
        "items_of", "num", "parse_time", "hash_key", "_url_host", "has_keep_tag",
        "has_hr_tag", "file_ref", "state_group", "group_allowed", "seeded_hours",
        "seeded_start", "removal_blocker", "library_verdict", "units_text",
        "as_relations", "uniq", "uniq_units", "empty_relations", "ingest_entry",
        "ledger_add", "ledger_trim", "relations_from_ledger", "should_remind",
        "open_until", "recently_opened",
    }
    WANTED_NAMES = {
        "KEEP_TAGS", "HR_TAGS", "_STATE_GROUPS", "GROUP_TITLES", "_WAITING",
        "_IMPORTED", "_UNKNOWN_STATUS_KEEP", "_GONE_FILE_STATES",
        "UNFINISHED_GRACE_DAYS", "WEB_IDLE_MINUTES", "MAX_PER_RUN",
        "SEEN_LIMIT", "HISTORY_SIZE", "LEDGER_LIMIT", "TAG_KEEP",
    }

    _tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    _body = []
    for _node in _tree.body:
        if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _node.name in WANTED_FUNCS:
                _body.append(_node)
        elif isinstance(_node, ast.Assign):
            _targets = {t.id for t in _node.targets if isinstance(t, ast.Name)}
            if _targets and _targets <= WANTED_NAMES:
                _body.append(_node)
        elif isinstance(_node, ast.AnnAssign) and isinstance(_node.target, ast.Name):
            if _node.target.id in WANTED_NAMES:
                _body.append(_node)
    _module = ast.fix_missing_locations(ast.Module(body=_body, type_ignores=[]))

    def _stub(name: str, **attrs):
        created = types.ModuleType(name)
        for _key, _value in attrs.items():
            setattr(created, _key, _value)
        sys.modules[name] = created
        return created

    class _Field:
        def __init__(self, default=None, **_kwargs):
            self.default = default

    class _BaseModel:
        pass

    class _SecretStr(str):
        pass

    class _OpsError(Exception):
        def __init__(self, message="", **_kwargs):
            super().__init__(message)
            self.message = message

    def _field_validator(*_args, **_kwargs):
        def _wrap(func):
            return func

        return _wrap

    _stub(
        "pydantic",
        BaseModel=_BaseModel,
        Field=_Field,
        SecretStr=_SecretStr,
        ConfigDict=lambda **kwargs: kwargs,
        field_validator=_field_validator,
    )
    _stub("fastapi", APIRouter=lambda **_kwargs: None, HTTPException=Exception)
    _stub("movieclaw_api")
    _stub("movieclaw_api.plugins")
    _stub("movieclaw_api.plugins.keys", PLUGIN_DATA="data", PLUGIN_ROUTES="routes")
    _stub(
        "movieclaw_scheduler",
        SCHEDULED_TASKS=object(),
        TaskDefinition=object(),
        TriggerType=object(),
    )
    _stub("movieclaw_sdk", Context=object, plugin=lambda *_a, **_k: (lambda f: f))
    _stub(
        "movieclaw_sdk.events",
        DOWNLOAD_COMPLETED="download.completed",
        LIBRARY_INGEST_COMPLETED="library.ingest.completed",
        LIBRARY_ITEM_VIEWED="library.item.viewed",
        PLAYBACK_STARTED="playback.started",
        PLUGIN_EVENTS="plugin_events",
    )
    _stub("movieclaw_sdk.events.plugin", plugin_event=lambda *_a, **_k: None)
    _stub(
        "movieclaw_sdk.jobs",
        JOB_HANDLERS="jobs",
        PLUGIN_JOBS="plugin_jobs",
        JobBlocked=Exception,
        RegisteredJobHandler=object,
    )
    _stub("movieclaw_sdk.ops", HOST_OPS="ops", OpsError=_OpsError)

    _target = types.ModuleType("seed_scheduler")
    _target.__file__ = str(SOURCE)
    # exec 之前先把纯函数注解里会用到的名字放进模块命名空间
    _target.asyncio = asyncio
    _target.datetime = datetime
    _target.timezone = timezone
    _target.urlsplit = urlsplit
    _target.pydantic = sys.modules["pydantic"]
    _target.Field = _Field
    _target.SecretStr = _SecretStr
    _target.field_validator = _field_validator
    _target.APIRouter = lambda **_kwargs: None
    _target.HTTPException = Exception
    _target.PLUGIN_DATA = "data"
    _target.PLUGIN_ROUTES = "routes"
    _target.SCHEDULED_TASKS = object()
    _target.TaskDefinition = object()
    _target.TriggerType = object()
    _target.Context = object
    _target.plugin = lambda *_a, **_k: (lambda f: f)
    _target.DOWNLOAD_COMPLETED = "download.completed"
    _target.LIBRARY_INGEST_COMPLETED = "library.ingest.completed"
    _target.LIBRARY_ITEM_VIEWED = "library.item.viewed"
    _target.PLAYBACK_STARTED = "playback.started"
    _target.PLUGIN_EVENTS = "plugin_events"
    _target.plugin_event = lambda *_a, **_k: None
    _target.JOB_HANDLERS = "jobs"
    _target.PLUGIN_JOBS = "plugin_jobs"
    _target.JobBlocked = Exception
    _target.RegisteredJobHandler = object
    _target.HOST_OPS = "ops"
    _target.OpsError = _OpsError
    _target.Settings = type("Settings", (), {})

    sys.modules["seed_scheduler"] = _target
    exec(compile(_module, str(SOURCE), "exec"), _target.__dict__)