"""启动自动恢复：面板重启后重新拉起「登记为 RUNNING 但进程已死」的实例。

背景（2026-09-26）：实例进程是面板的子进程，`systemctl restart dicemanager`
会连带把它们全部杀掉，而 registry 里状态仍是 RUNNING——此前没有任何恢复逻辑，
表现为「面板显示在跑、实际全部离线」，每次都得人工逐个点启动。

安全边界（避免「不该拉的拉起来」）：
- 只认 State.RUNNING。用户主动 stop 会把状态落回 CONFIGURED（见 rest.py
  instance_op），因此不会被强行拉起；AWAIT_LOGIN 需要扫码交互，也不自动拉；
  ERROR 态交给用户排查。RUNNING 是唯一「用户明确表达过要它一直跑」的状态。
- 逐实例独立 try/except：一个实例失败（目录被删、二进制缺失）不影响其余。
- `DM_AUTO_RESUME=0` 可整体关闭（排障时不希望被自动拉起干扰）。
- 每个失败的实例都会把原因写进它自己的实例日志（pm.note），面板日志 UI 可见。
"""
import logging
import os
import time
from typing import Callable

from core.locks import instance_lock
from core.registry import State

log = logging.getLogger("dicemanager.resume")

DEFAULT_DELAY = 2.0        # 等 uvicorn 先把端口跑起来，避免启动期抢占资源
DEFAULT_STAGGER = 1.5      # 实例之间错开，避免同时拉起抢 CPU / 端口 / 登录

_OFF = {"0", "false", "no", "off"}


def _enabled() -> bool:
    return os.environ.get("DM_AUTO_RESUME", "1").strip().lower() not in _OFF


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def resume_running_instances(registry, pm, start_fn: Callable[[str], object],
                             logger: logging.Logger | None = None,
                             delay: float | None = None,
                             stagger: float | None = None) -> dict[str, str]:
    """把面板重启前处于 RUNNING 的实例重新拉起。

    registry / pm 取 API 侧同名对象；start_fn(iid) 复用 Wizard.start_instance
    （与 REST start、向导 step5 完全同一条路径，杜绝第二个启动实现漂移）。
    返回 {实例id: "alive" | "started" | "error: xxx"} 便于测试与排障。
    """
    lg = logger or log
    result: dict[str, str] = {}
    if not _enabled():
        lg.info("[resume] DM_AUTO_RESUME 已关闭，跳过实例自动拉起")
        return result

    targets = [r["id"] for r in registry.all()
               if r.get("state") == State.RUNNING.value]
    if not targets:
        lg.info("[resume] 无 RUNNING 实例需要恢复")
        return result

    d = delay if delay is not None else _float_env("DM_RESUME_DELAY", DEFAULT_DELAY)
    if d:
        time.sleep(d)                       # 让 HTTP 服务先可用

    st = stagger if stagger is not None else _float_env("DM_RESUME_STAGGER",
                                                        DEFAULT_STAGGER)
    pending: list[str] = []
    for iid in targets:
        try:
            if pm.get(iid).is_alive():
                result[iid] = "alive"       # 进程还在（本机连跑两个面板时可能出现）
                continue
        except Exception as e:              # 探测失败按「需要拉起」处理，由 start_fn 给真实原因
            lg.warning("[resume] 探测实例 %s 状态时异常：%s", iid, e)
        pending.append(iid)
    if not pending:
        return result

    lg.info("[resume] 面板重启后待恢复实例：%s", ", ".join(pending))
    for i, iid in enumerate(pending):
        if i and st:
            time.sleep(st)
        try:
            with instance_lock(iid):               # 与 REST start 同口径串行化
                start_fn(iid)
            pm.get(iid).note("面板重启后已自动恢复运行")
            result[iid] = "started"
            lg.info("[resume] 实例 %s 已自动拉起", iid)
        except Exception as e:
            result[iid] = f"error: {e}"
            lg.warning("[resume] 实例 %s 自动拉起失败：%s", iid, e)
            try:
                pm.get(iid).note(f"面板重启后自动恢复失败：{e}")
            except Exception:
                pass                        # 连日志都写不了时不要掩盖上层结论
    return result
