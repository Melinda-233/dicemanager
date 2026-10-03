"""角色派生：登录端 vs 应用端（骰子端）。

口径说明（**唯一权威定义**，与前端 web/src/pages/Overview.vue 和
tests/test_manifest_integrity.py::test_role_split_is_consistent 必须一致）：

    登录端 = 出现在任意骰子端 manifest 的 compatible_login 列表里的程序名（排除 builtin）

派生而非落盘：程序增减只改 manifest，加一个 Instance.role 冗余字段就得同步迁移
存量数据，且容易与 manifest 漂移。此处只读 manifest，天然跟随清单变化。

缓存：配额统计 usage() 会对全量实例反复调用本函数，故按注册表身份记忆化——
适配器注册表在进程内是启动期一次性加载的静态对象，不会中途变化。
"""
import threading

__all__ = ["is_login_program", "login_programs", "app_programs"]

_cache: dict[int, set[str]] = {}
_cache_guard = threading.Lock()

def _cache_key(adapters: dict) -> int:
    return id(adapters) ^ (len(adapters) << 16)

def login_programs(adapters: dict) -> set[str]:
    """全部登录端程序名集合。

    adapters 与 api.context.ctx.adapters 同形：{name: (manifest, cls)}。
    """
    key = _cache_key(adapters)
    with _cache_guard:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    out: set[str] = set()
    for manifest, _ in adapters.values():
        for name in (manifest.get("compatible_login") or []):
            if name and name != "builtin":
                out.add(name)
    with _cache_guard:
        _cache[key] = out
    return out

def is_login_program(dice: str, adapters: dict) -> bool:
    """该程序是登录端吗？口径见模块 docstring。"""
    return dice in login_programs(adapters)

def app_programs(adapters: dict) -> set[str]:
    """全部应用端（骰子端）程序名集合。"""
    return {name for name in adapters} - login_programs(adapters)
