"""Edition 开关：server（Linux 服务器）与 desktop（Windows 单机本地）的分化边界。

两版共享同一套内核（core/ api/ adapters/ services/ web/src），差异只允许长在
边缘的五个点上：

  C1 鉴权模型    server 多用户+配额；desktop 单用户 + 首启 WebUI 设密码
  C2 首启端点    desktop 有 /needs-setup、/setup
  C3 服务化      server 有 /panel/restart（systemd 拉起）
  C4 网络暴露    server 放通防火墙端口；desktop 只听 127.0.0.1（no-op）
  C5 路径策略    server 走 /var/lib 等固定部署位；desktop 走项目根 data/

差异化代码必须可 grep，方便审计两版究竟差在哪：

    grep -rn "edition\\." core api adapters services

DM_EDITION 可显式覆盖（测试用）；缺省按 os.name 推断。
"""
import os

EDITION_SERVER = "server"
EDITION_DESKTOP = "desktop"

_VALID = (EDITION_SERVER, EDITION_DESKTOP)


def _default() -> str:
    """缺省推断：Windows → desktop，其余（Linux 服务器）→ server。"""
    return EDITION_DESKTOP if os.name == "nt" else EDITION_SERVER


def _resolve() -> str:
    v = (os.environ.get("DM_EDITION") or "").strip().lower()
    return v if v in _VALID else _default()


EDITION = _resolve()


def is_server() -> bool:
    return EDITION == EDITION_SERVER


def is_desktop() -> bool:
    return EDITION == EDITION_DESKTOP
