"""adb 网络客户端的薄封装（adb-shell 0.4.3）。

这一层只管「连上、执行命令、推文件」；业务判断在 operations.py。
adb_shell 在运行期才导入（服务器按锁文件装依赖）；单测用假链接，不导入它。
"""

import os
import tempfile


# 连接失败的几种形态：路由层按 kind 给用户能看懂的话
KIND_REFUSED = "refused"  # 端口没开：电视上还没打开网络调试
KIND_AUTH_WAIT = "waiting_auth"  # 电视上弹了「允许调试」，还没点
KIND_TIMEOUT = "timeout"  # 网络慢 / 电视休眠
KIND_OTHER = "other"


class AdbError(Exception):
    """带 kind 的 adb 失败。kind 供路由层决定说什么。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def make_signer(priv_pem: str, pub: str):
    """用插件私有目录里的密钥内容造认证签名器（内容而不是路径，方便走文件服务）。"""
    from adb_shell.auth.sign_pythonrsa import PythonRSASigner

    return PythonRSASigner(pub=pub, priv=priv_pem)


def generate_keypair() -> tuple[bytes, bytes]:
    """生成一对 ADB 调试密钥，返回 (私钥 PEM, 公钥)。

    keygen 只认真实路径，借临时目录生成后读出内容；持久化由调用方（文件服务）负责。
    """
    from adb_shell.auth.keygen import keygen

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "adbkey")
        keygen(path)
        with open(path, "rb") as f:
            priv = f.read()
        with open(path + ".pub", "rb") as f:
            pub = f.read()
    return priv, pub


class AdbLink:
    """一台电视的 adb 连接。同步阻塞；路由层用 to_thread 包起来。"""

    def __init__(self, host: str, port: int = 5555, signer=None, banner: str = "movieclaw-tv-home"):
        self._host = host
        self._port = port
        self._signer = signer
        self._banner = banner
        self._device = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def connect(self, auth_timeout_s: float = 60.0) -> None:
        from adb_shell.adb_device import AdbDevice
        from adb_shell.transport.tcp_transport import TcpTransport

        transport = TcpTransport(self._host, self._port)
        try:
            transport.connect(transport_timeout_s=10.0)
        except ConnectionRefusedError as e:
            raise AdbError(
                KIND_REFUSED,
                f"电视 {self._host}:{self._port} 拒绝连接：先在电视上打开开发者选项里的「网络调试 / ADB 调试」",
            ) from e
        except OSError as e:
            raise AdbError(KIND_TIMEOUT, f"电视 {self._host}:{self._port} 连不上（{e.__class__.__name__}）") from e

        device = AdbDevice(transport, default_transport_timeout_s=10.0, banner=self._banner)
        try:
            device.connect(
                rsa_keys=[self._signer] if self._signer is not None else None,
                transport_timeout_s=10.0,
                auth_timeout_s=auth_timeout_s,
            )
        except Exception as e:  # AdbTimeoutError / DeviceAuthError / TcpTimeoutException
            transport.close()
            raise self._map_connect_error(e) from e
        self._device = device

    @staticmethod
    def _map_connect_error(e: Exception) -> AdbError:
        from adb_shell.exceptions import DeviceAuthError

        if isinstance(e, DeviceAuthError):
            return AdbError(KIND_AUTH_WAIT, f"电视拒绝了认证：在电视上删除旧授权后重试（{e}）")
        if isinstance(e, TimeoutError) or "timeout" in e.__class__.__name__.lower() or "Timeout" in type(e).__name__:
            return AdbError(
                KIND_AUTH_WAIT,
                "连接没完成就超时了：如果电视上弹了「允许调试吗」，点允许并勾选「始终允许」，然后再调一次 connect；"
                "如果没弹框，确认电视没在休眠、和服务器在同一网络",
            )
        return AdbError(KIND_OTHER, f"连接电视失败：{e}")

    def shell(self, command: str, timeout_s: float = 60.0) -> str:
        if self._device is None:
            raise AdbError(KIND_OTHER, "还没连接电视")
        try:
            return self._device.shell(command, timeout_s=timeout_s, decode=True)
        except AdbError:
            raise
        except Exception as e:
            raise AdbError(KIND_OTHER, f"电视上的命令失败了：{command}\n{e}") from e

    def push(self, local_path: str, device_path: str) -> None:
        if self._device is None:
            raise AdbError(KIND_OTHER, "还没连接电视")
        try:
            self._device.push(local_path, device_path)
        except AdbError:
            raise
        except Exception as e:
            raise AdbError(KIND_OTHER, f"推送 {device_path} 失败：{e}") from e

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
