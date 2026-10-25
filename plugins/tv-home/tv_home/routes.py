"""把电视桌面的接口挂到插件路由上（管理员区），agent 经 ``mclaw plugins tv-home …`` 调用。

职责只有三样：插件私有文件的异步读写（记录、调试密钥）、MovieClaw 主 App 安装包的下载校验、
把同步的设备操作放进线程并按电视串行。判断都在 operations.py。

状态记录按设计稿 §8.6 存为 ``<序列号>.json``；序列号要连上电视才拿得到，
所以读取时按 host 字段匹配（记录里存着 host），写入用操作返回的序列号。
"""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from movieclaw_sdk.net import http_transport

from . import operations as ops
from .adb import AdbError, AdbLink, generate_keypair, make_signer
from .records import merge_record, rescue_text

SERVICE_NAME = "tv-home"
RELEASE_API = "https://api.github.com/repos/movieclaw/MovieClaw/releases"
MAIN_APK_ASSET = "MovieClaw-AndroidTV.apk"

# GitHub 上的下载（主 App、aerial-tv）借用宿主的 github 服务路由：用户多半已为它配好代理
# （国内部署常见「仅 github 走代理」）；插件自己的服务名那条路由是直连，会撞墙。
GITHUB_SERVICE = "github"
AERIAL_RELEASES_API = "https://api.github.com/repos/pengchujin/aerial-tv/releases"
AERIAL_ASSET = "aerial-standard.apk"

GUIDE = """先调本接口（guide）读操作说明，再按顺序调：connect → probe → install → home → verify → close。

- 顺序执行，每一步把返回里的 say 原样转述给用户；失败就停下，不重试、不换别的办法。
- 设备上的一切改动只通过本插件的接口；不要用 bash 自己连电视，不要拼 pm / settings / am 命令。
- 只问用户三类事：遥控器上的手动步骤、看屏幕确认、要不要继续。
- 桌面壳有两种：默认装自带的极简壳「MovieClaw 桌面」；用户想要 Apple TV 风格就 install 和 home 都带 shell=aerial
  （装 aerial-tv，MIT 许可，含壁纸与航拍屏保，走它的 GitHub 正式版并校验哈希）。两种壳的换桌面与还原流程完全一样。
- connect 返回 waiting_auth 时：电视上弹了「允许调试吗」，请用户点允许并勾选「始终允许」，点完再调一次 connect。
- probe 的 level 是 B（或 C）时只换桌面、不去臃肿；把 level_note 告诉用户。
- MovieClaw 没装时先调 install（可用 main_apk_url 给正式版地址，或不传参让插件取最新正式版；装不了就说明原因）。
- home 成功后：请用户按一下真遥控器的主页键，说屏幕上是什么。真遥控器回不到新桌面就停下，把结果原样转述。
- 全部做完必须调 close（先请用户在电视上关掉调试），并告诉用户怎么还原：
  调 restore 即可；厂商桌面没被停用的机型，也可以直接在系统设置里卸载桌面壳。
- 用户想卸载桌面壳时：先调 restore（或确认厂商桌面没被停用），绝不先卸桌面壳——
  厂商桌面还停用着就卸桌面壳，屏幕会黑、遥控器救不回来。
"""


class HostBody(BaseModel):
    host: str = Field(min_length=3, description="电视的 IP")
    port: int = Field(default=5555, ge=1, le=65535)


class InstallBody(HostBody):
    main_apk_url: "str | None" = Field(default=None, description="MovieClaw 主 App 的 APK 地址；不填就取最新正式版")
    main_apk_sha256: "str | None" = Field(default=None, description="APK 的 sha256（十六进制），下载后校验")
    shell: str = Field(default="movieclaw", description="桌面壳：movieclaw（默认，随插件包）或 aerial（aerial-tv，tvOS 风格）")


class HomeBody(HostBody):
    shell: str = Field(default="movieclaw", description="哪种桌面壳；与 install 时一致")


class RestoreBody(HostBody):
    uninstall_main: bool = Field(default=False, description="同时卸载 MovieClaw 主 App（默认保留）")


class TvStore:
    """插件私有文件里的持久化：调试密钥、每台电视的状态记录与救援命令。"""

    def __init__(self, files):
        self._files = files  # ctx.use(PLUGIN_FILES).scoped(ctx)

    async def read_bytes(self, name: str) -> "bytes | None":
        path = self._files.path("plugin", name)
        if not await self._files.exists(path):
            return None
        handle = await self._files.open(path, "rb")
        try:
            return handle.read()
        finally:
            handle.close()

    async def write_bytes(self, name: str, data: bytes) -> None:
        path = self._files.path("plugin", name)
        handle = await self._files.open(path, "wb")
        try:
            handle.write(data)
        finally:
            handle.close()

    async def ensure_keys(self) -> tuple:
        """调试密钥（一对）：没有就生成。密钥只在插件数据目录里（设计稿 §12）。"""
        priv = await self.read_bytes("adbkey")
        pub = await self.read_bytes("adbkey.pub")
        if priv and pub:
            return priv, pub
        priv, pub = await asyncio.to_thread(generate_keypair)
        await self.write_bytes("adbkey", priv)
        await self.write_bytes("adbkey.pub", pub)
        return priv, pub

    async def signer(self):
        priv, pub = await self.ensure_keys()
        return await asyncio.to_thread(make_signer, priv.decode(), pub.decode())

    async def load_record_for_host(self, host: str) -> "dict | None":
        """按 host 找这台电视的记录（文件名是序列号，只有连上才拿得到）。"""
        path = self._files.path("plugin")
        best = None
        for name in await self._files.listdir(path):
            if not name.endswith(".json") or name.endswith("-rescue.json"):
                continue
            raw = await self.read_bytes(name)
            try:
                record = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(record, dict) and record.get("host") == host:
                if best is None or record.get("updated_at", "") > best.get("updated_at", ""):
                    best = record
        return best

    async def save_record(self, serial: str, record: dict) -> None:
        await self.write_bytes(f"{serial}.json", json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8"))

    async def save_rescue(self, serial: str, record: dict, notes: "list[str] | None") -> None:
        await self.write_bytes(f"{serial}-rescue.txt", rescue_text(record, notes or []).encode("utf-8"))


_locks: dict = {}


def _lock_for(host: str) -> asyncio.Lock:
    if host not in _locks:
        _locks[host] = asyncio.Lock()
    return _locks[host]


async def _with_link(store: TvStore, host: str, port: int, fn, *args):
    """开一条 adb 链接跑一个同步操作：密钥就绪、按电视串行、线程里跑、走完就关。"""
    signer = await store.signer()
    link = AdbLink(host, port, signer)
    try:
        async with _lock_for(host):
            return await asyncio.to_thread(_connect_and_run, link, fn, *args)
    except AdbError as e:
        raise HTTPException(status_code=502, detail=e.message) from e
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    finally:
        await asyncio.to_thread(link.close)


def _connect_and_run(link: AdbLink, fn, *args):
    link.connect()
    return fn(link, *args)


def _shell_apk() -> bytes:
    """桌面壳 APK 随插件包发：发版流程把它放进 apk/ 再打包（不入库）。"""
    path = Path(__file__).resolve().parent.parent / "apk" / "MovieClawHome.apk"
    if not path.is_file():
        raise HTTPException(
            status_code=500,
            detail="插件包里没有 apk/MovieClawHome.apk：这包发版时没有打进取桌面壳。重新打包或先升级插件。")
    return path.read_bytes()


def _ok(say: str, **data) -> dict:
    return {"ok": True, "say": say, **data}


def _result(res: dict) -> dict:
    """operations 的业务失败（ok=False，如判为 C 级）用 409 表达；say 让 agent 原样转述。"""
    res = dict(res)
    ok = res.pop("ok", True)
    if not ok:
        raise HTTPException(status_code=409, detail=res.pop("say", "操作没有完成"))
    return res


async def _latest_main_apk(client: httpx.AsyncClient) -> tuple:
    """最新正式版的 AndroidTV APK：地址与 sha256（忽略 draft / 预发布，§2 安装包来源）。"""
    releases = (await client.get(RELEASE_API, params={"per_page": 40})).json()
    if not isinstance(releases, list):
        raise HTTPException(status_code=502, detail="GitHub Release 列表拿不到，稍后再试或直接给 main_apk_url")
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        for asset in rel.get("assets", []):
            if asset.get("name") == MAIN_APK_ASSET:
                digest = asset.get("digest") or ""
                sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                return asset["browser_download_url"], sha, rel.get("tag_name", "")
    raise HTTPException(status_code=502, detail="最近的正式 Release 里没有 AndroidTV 安装包，直接给 main_apk_url")


async def _download_checked(client: httpx.AsyncClient, url: str, expected: "str | None", origin: str) -> bytes:
    """下载并按 sha256 校验（期望值缺了就拒绝装）。"""
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"下载 {origin} 失败：HTTP {resp.status_code}")
    data = resp.content
    if expected and hashlib.sha256(data).hexdigest() != expected.lower():
        raise HTTPException(status_code=502, detail=f"{origin} 的 sha256 对不上，拒绝安装")
    if not expected:
        raise HTTPException(status_code=422,
                            detail=f"拿不到 {origin} 的 sha256，拒绝安装：请给哈希，或改用上传")
    return data


async def _fetch_main_apk(body: InstallBody) -> tuple:
    """下载主 App 安装包并校验 sha256（走宿主统一出口）。返回 (字节, 来源说明)。"""
    async with httpx.AsyncClient(transport=http_transport(GITHUB_SERVICE), timeout=600.0,
                                 follow_redirects=True) as client:
        if body.main_apk_url:
            url, origin, expected = body.main_apk_url, body.main_apk_url, body.main_apk_sha256
        else:
            url, digest, tag = await _latest_main_apk(client)
            origin, expected = f"{tag} 的 {MAIN_APK_ASSET}", body.main_apk_sha256 or digest
        data = await _download_checked(client, url, expected, origin)
    return data, origin


async def _fetch_aerial_apk() -> tuple:
    """aerial-tv（MIT）最新正式版的 aerial-standard.apk，GitHub digest 校验。

    guard 变体不装——那是它给「拒装 HOME 包」机型的接管方案，对应我们的 P4。"""
    async with httpx.AsyncClient(transport=http_transport(GITHUB_SERVICE), timeout=600.0,
                                 follow_redirects=True) as client:
        releases = (await client.get(AERIAL_RELEASES_API, params={"per_page": 20})).json()
        if not isinstance(releases, list):
            raise HTTPException(status_code=502, detail="aerial-tv 的 Release 列表拿不到，稍后再试")
        for rel in releases:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            for asset in rel.get("assets", []):
                if asset.get("name") == AERIAL_ASSET:
                    digest = asset.get("digest") or ""
                    sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
                    url = asset["browser_download_url"]
                    origin = f"aerial-tv {rel.get('tag_name', '')} 的 {AERIAL_ASSET}"
                    data = await _download_checked(client, url, sha, origin)
                    return data, origin
    raise HTTPException(status_code=502, detail="aerial-tv 最近的正式 Release 里没有 aerial-standard.apk")


def build_router(store: TvStore) -> APIRouter:
    router = APIRouter()

    @router.get("/guide", operation_id="plugins.tv-home.guide", summary="给 agent 的操作说明")
    async def guide() -> dict:
        return {"text": GUIDE}

    @router.post("/connect", operation_id="plugins.tv-home.connect", summary="连接电视（第一次会弹授权）")
    async def connect(body: HostBody) -> dict:
        signer = await store.signer()
        link = AdbLink(body.host, body.port, signer)
        try:
            async with _lock_for(body.host):
                await asyncio.to_thread(link.connect)
        except AdbError as e:
            if e.kind == AdbError.KIND_AUTH_WAIT:
                return _ok(e.message, status="waiting_auth")
            raise HTTPException(status_code=502, detail=e.message) from e
        finally:
            await asyncio.to_thread(link.close)
        return _ok(f"已连上 {body.host}。接着调 probe 摸底。", status="connected")

    @router.post("/probe", operation_id="plugins.tv-home.probe", summary="只读摸底：型号、固件、HOME 候选")
    async def probe(body: HostBody) -> dict:
        res = await _with_link(store, body.host, body.port, ops.probe, body.host)
        return _ok(res.pop("level_note", "摸底完成"), **res)

    @router.post("/install", operation_id="plugins.tv-home.install",
                 summary="装桌面壳（shell=movieclaw 默认 / aerial）；MovieClaw 主 App 用 main_apk_url（默认取最新正式版）")
    async def install(body: InstallBody) -> dict:
        if body.shell not in ops.SHELL_PKGS:
            raise HTTPException(status_code=422, detail=f"shell 只支持 {'、'.join(ops.SHELL_PKGS)}")
        main_bytes, origin = None, None
        if body.main_apk_url is not None or body.main_apk_sha256 is not None:
            main_bytes, origin = await _fetch_main_apk(body)
        if body.shell == "movieclaw":
            shell_bytes, shell_note = _shell_apk(), "随插件包"
        else:
            shell_bytes, shell_note = await _fetch_aerial_apk()
        record = await store.load_record_for_host(body.host)

        def fn(link):
            snap = ops.snapshot(link, body.host)
            if main_bytes is not None:
                ops.install_main(link, main_bytes)
            ops.install_shell(link, shell_bytes, body.shell)
            return snap

        snap = await _with_link(store, body.host, body.port, fn)
        record = merge_record(record, snap)
        await store.save_record(snap["serial"], record)
        say = ("MovieClaw 主 App 已装上（来源：" + origin + "）。" if main_bytes else "") \
            + f"桌面壳（{ops.SHELL_PKGS[body.shell][0]}，{shell_note}）已装好，接着调 home（shell={body.shell}）换桌面。"
        return _ok(say, serial=snap["serial"])

    @router.post("/install-apk", operation_id="plugins.tv-home.install-apk",
                 summary="装桌面壳 + 请求体上传的 MovieClaw 主 App APK（octet-stream）")
    async def install_apk(request: Request, host: str, port: int = 5555,
                          sha256: "str | None" = None) -> dict:
        data = await request.body()
        if not data:
            raise HTTPException(status_code=422, detail="请求体是空的：把 APK 按原始字节传上来")
        if sha256 and hashlib.sha256(data).hexdigest() != sha256.lower():
            raise HTTPException(status_code=502, detail="上传的 APK sha256 对不上，拒绝安装")
        record = await store.load_record_for_host(host)

        def fn(link):
            snap = ops.snapshot(link, host)
            ops.install_main(link, data)
            ops.install_shell(link, _shell_apk())
            return snap

        snap = await _with_link(store, host, port, fn)
        record = merge_record(record, snap)
        await store.save_record(snap["serial"], record)
        return _ok("MovieClaw 主 App（上传的 APK）与桌面壳都装好了，接着调 home 换桌面。", serial=snap["serial"])

    @router.post("/home", operation_id="plugins.tv-home.home", summary="换桌面（七步，失败回滚）")
    async def home(body: HomeBody) -> dict:
        if body.shell not in ops.SHELL_PKGS:
            raise HTTPException(status_code=422, detail=f"shell 只支持 {'、'.join(ops.SHELL_PKGS)}")
        shell_apk = _shell_apk() if body.shell == "movieclaw" else None
        record = await store.load_record_for_host(body.host)
        res = await _with_link(store, body.host, body.port, ops.run_home, record, shell_apk, body.shell)
        await store.save_record(res["serial"], res["record"])
        if res.get("rescue"):
            await store.save_rescue(res["serial"], res["record"], None)
        return _result(res)

    @router.post("/verify", operation_id="plugins.tv-home.verify", summary="汇总现状与记录是否一致")
    async def verify(body: HostBody) -> dict:
        record = await store.load_record_for_host(body.host)
        res = await _with_link(store, body.host, body.port, ops.run_verify, record)
        return _result(res)

    @router.post("/restore", operation_id="plugins.tv-home.restore", summary="按状态记录还原")
    async def restore(body: RestoreBody) -> dict:
        record = await store.load_record_for_host(body.host)
        res = await _with_link(store, body.host, body.port, ops.run_restore, record, body.uninstall_main)
        return _result(res)

    @router.post("/close", operation_id="plugins.tv-home.close", summary="确认调试端口已关")
    async def close(body: HostBody) -> dict:
        closed = await asyncio.to_thread(ops.check_port_closed, body.host, body.port)
        if closed:
            return _ok(f"{body.host}:{body.port} 已拒绝连接，调试关好了。这单可以收尾。", status="closed")
        raise HTTPException(status_code=409,
                            detail=f"{body.host}:{body.port} 没有立刻拒绝连接：端口可能还开着。"
                                   f"请用户在电视上关掉「网络调试 / ADB 调试」后再调一次 close")

    return router
