"""青果 OlivaDice：OPK 组合部署（缺核阻断 / 缺件告警）"""
import urllib.request
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult, mirror_url


class OlivaDiceAdapter(BaseAdapter):
    # 整合包（内置客户端，无外部 WS 端口）：进程存活即视为已连接
    HEALTH_PORT_KEYS: list[str] = []
    def deploy(self, instance) -> str:
        from core.locks import program_dir_lock
        with program_dir_lock("olivadice"):
            app = Path(instance.dir) / self.m["opk_dir"]
            app.mkdir(parents=True, exist_ok=True)
            for mod in self.m["opk_modules"]:
                dest = app / f"{mod}.opk"
                if not dest.exists():
                    url = self.m["download_opk_pattern"].format(mod=mod)
                    #走 mirror_url：GitHub 下载失败时按探测结果切镜像
                    dest.write_bytes(
                        urllib.request.urlopen(mirror_url(url), timeout=300).read())
            missing = self.verify_required(instance)
            if any("OlivaDiceCore" in f for f in missing):
                raise RuntimeError("OlivaDiceCore.opk 缺失，阻断启动")   # 缺核阻断
            if missing:
                instance.warnings = [f"缺失子模块: {missing}"]           # 缺件告警
        return "ok"

    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]       # OlivOS 启动入口

    def configure_login(self, instance, credentials) -> dict:
        return {"needs_login": False}                          # 整合包内置登录

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        return WriteResult(ok=True)                            # 内置客户端无外部 WS
