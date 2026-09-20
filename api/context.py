"""AppContext：依赖注入容器 + 适配器实例缓存（模块级单例，import 即初始化）"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from adapters import load_registry
from core.ports import PortAllocator
from core.process import ProcessManager
from core.registry import Registry
from services.login import LoginService
from services.wizard import Wizard

# 默认 Linux 生产路径；Windows 开发 / 非 root 运行可用 DM_STATE_DIR / DM_LOG_DIR 覆盖
STATE_DIR = Path(os.environ.get("DM_STATE_DIR", "/var/lib/dicemanager"))
LOG_DIR = Path(os.environ.get("DM_LOG_DIR", "/var/log/dicemanager"))

@dataclass
class AppContext:
    registry: Registry
    ports: PortAllocator
    pm: ProcessManager
    wizard: Wizard
    login: LoginService
    adapters: dict
    log_dir: Path
    resmon_alert: float = 0.90          # ≥90% 告警（可配置）
    resmon_warn: float = 0.80           # 部署预估黄牌 80%
    _adapter_cache: dict = field(default_factory=dict)

    def get_adapter(self, name: str):
        if name not in self._adapter_cache:
            manifest, cls = self.adapters[name]
            self._adapter_cache[name] = cls(manifest)
        return self._adapter_cache[name]

def build_context() -> AppContext:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    registry = Registry(STATE_DIR / "instances.json")
    ports = PortAllocator(STATE_DIR / "ports.json")
    pm = ProcessManager(LOG_DIR)
    adapters = load_registry(Path(__file__).parent.parent / "manifests")
    wizard = Wizard(registry, adapters, ports, pm, LOG_DIR)
    return AppContext(registry=registry, ports=ports, pm=pm, wizard=wizard,
                      login=LoginService(wizard, pm), adapters=adapters, log_dir=LOG_DIR)

# 模块级单例：import 时构建一次，所有模块拿到的是同一个实例
# （此前 lifespan 里 global ctx 只改 app.py 自身名字空间，其余模块拿不到 —— 已修正）
ctx = build_context()
