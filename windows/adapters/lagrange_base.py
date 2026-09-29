"""Lagrange.OneBot 与 Lagrange.Milky 的公共基类。

两者都是 LagrangeDev/Lagrange.Core 的下游实现，角色都是登录端，行为高度同构：
- 缺 appsettings.json 时程序会 Console.ReadKey(true) 等按键（无头部署卡死），故部署阶段就预置配置；
- 二维码以 Unicode 方块字符画打到 stdout，同时落盘 qr-{Uin}.png（基类「日志里找 data:」正则抓不到）；
- 登录成功后 keystore.json 落盘 UIN，日志锚点 "Bot Uin:"。
仅互联通道（ob11 Implementations[] vs Milky.HttpServer）、token 路径、配置合并策略不同，
由各自子类实现（子类必须提供 _fill_defaults）。
"""
import re
from abc import abstractmethod
from pathlib import Path

from adapters.base import BaseAdapter
from core.atomicio import atomic_write_json

# 二维码字符画整行（兼容模式为 ASCII 的 . ^ @）
QR_ART_RE = re.compile(r"^[▄▀█.^@ ]{16,}$")
BOT_UIN_RE = re.compile(r"Bot\s*Uin:?\s*(\d{5,12})", re.I)
KEYSTORE_UIN_RE = re.compile(r'"Uin"\s*:\s*(\d{5,12})')


class LagrangeBase(BaseAdapter):
    # ---------- 路径 ----------
    def _config(self, instance) -> Path:
        return Path(instance.dir) / self.m.get("config_path", "appsettings.json")

    # ---------- 部署 ----------
    def deploy(self, instance) -> str:
        """部署完即预置配置：缺 appsettings.json 会让程序停在 Console.ReadKey。

        不能只靠 prepare_start——扫码登录时进程在 Step3 就可能被拉起，早于 Step5。
        """
        result = super().deploy(instance)
        if result in ("ok", "conflict"):       # conflict=目录已存在（断点续跑）
            self._ensure_config(instance)
            self._ensure_executable(instance)
        return result

    def _ensure_config(self, instance) -> None:
        """补齐缺省键但不覆盖用户已改过的配置（合并策略由子类 _fill_defaults 决定）。"""
        try:
            atomic_write_json(self._config(instance), self._fill_defaults)
        except (OSError, ValueError):          # 预置失败不阻断部署，启动时还会再兜一次
            pass

    @staticmethod
    @abstractmethod
    def _fill_defaults(cfg: dict) -> dict:
        """配置骨架的合并策略，子类必须实现（两个前端的语义有意不同）。

        OneBot 用浅 setdefault，Milky 用深合并——因为 Milky 的默认值是多层嵌套的，
        浅合并会在用户只改了某个子键时丢掉它的兄弟键。这里只声明契约，不给默认实现，
        免得子类漏实现时「静默不补配置」而当场看不出来。
        """

    def _ensure_executable(self, instance) -> None:
        exe = Path(instance.dir) / self.m["exe"]
        try:
            if exe.exists() and not exe.stat().st_mode & 0o111:
                exe.chmod(exe.stat().st_mode | 0o755)   # tar 解压可能丢执行位
        except OSError:
            pass

    # ---------- 启动 ----------
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]

    def prepare_start(self, instance, runner=None) -> bool:
        """启动前兜底：配置文件与执行位（幂等，非首启一次性动作）。"""
        self._ensure_config(instance)
        self._ensure_executable(instance)
        return False

    # ---------- 二维码 ----------
    def extract_qrcode(self, line, instance=None):
        """命中字符画行 → 读盘 qr-*.png 回传；字符画有多行，相同图片由 ws_login 去重。"""
        if instance is None or not QR_ART_RE.match(line.rstrip()):
            return None
        return self._qr_from_disk(instance, "qr-*.png")

    # ---------- 账号回读 ----------
    def detect_account(self, instance) -> str | None:
        ks = Path(instance.dir) / "keystore.json"      # 登录成功后程序自己落盘
        if ks.exists():
            try:
                if m := KEYSTORE_UIN_RE.search(ks.read_text("utf-8", errors="ignore")):
                    return m.group(1)
            except OSError:
                pass
        return None

    @staticmethod
    def account_from_logs(lines) -> str | None:
        for _, line in reversed(list(lines)[-300:]):
            if m := BOT_UIN_RE.search(line):          # 日志锚点 "Bot Uin: 12345"
                return m.group(1)
        return None
