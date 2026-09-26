"""AppContext：依赖注入容器 + 适配器实例缓存（模块级单例，import 即初始化）"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from adapters import load_registry
from core.exports import exports_dir
from core.metrics import MetricsStore
from core.ports import PortAllocator
from core.process import ProcessManager
from core.registry import Registry
from core.scheduler import Scheduler
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
    adapters: dict
    log_dir: Path
    scheduler: Scheduler
    metrics: MetricsStore               # 资源时序（内存/CPU），采样线程在 app 里起
    resmon_alert: float = 0.90          # ≥90% 告警（可配置）
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
    # 启动时回收孤儿日志：历史上实例删除不同步删日志，反复增删会累积一批无归属文件。
    # 只看注册表存活 id，面板自身日志由 PANEL_LOG_STEM 排除。清理失败不影响启动。
    try:
        pm.sweep_orphan_logs({r["id"] for r in registry.all()})
    except Exception:
        pass
    wizard = Wizard(registry, adapters, ports, pm, LOG_DIR)
    # 定时备份与手动导出/升级前快照统一落在 exports/（口径与回收入口见 core/exports.py）
    scheduler = Scheduler(STATE_DIR / "schedules.json", exports_dir(),
                          registry, wizard)
    metrics = MetricsStore(STATE_DIR / "metrics")
    # 实例删除已同步删曲线；这里兜底回收历史上遗留的孤儿曲线文件
    try:
        metrics.prune_files({r["id"] for r in registry.all()})
    except Exception:
        pass
    return AppContext(registry=registry, ports=ports, pm=pm, wizard=wizard,
                      adapters=adapters, log_dir=LOG_DIR,
                      scheduler=scheduler, metrics=metrics)

# 模块级单例：import 时构建一次，所有模块拿到的是同一个实例
# （此前 lifespan 里 global ctx 只改 app.py 自身名字空间，其余模块拿不到 —— 已修正）
ctx = build_context()
