"""登录编排：登录进程承载 + WS 推送通道供给"""
from core.locks import instance_lock
from core.registry import State


class LoginService:
    def __init__(self, wizard, processes):
        self.w = wizard; self.pm = processes

    def ensure(self, instance_id: str, needs_login: bool, credentials: dict) -> dict:
        with instance_lock(instance_id):
            inst = self.w.reg.get(instance_id)
            adapter = self.w.get_adapter(inst.dice)
            r = adapter.configure_login(inst, credentials)
            if r.get("conflict"):
                return {"ok": False, "message": r["conflict"]}
            if needs_login:
                try:
                    self.pm.get(instance_id).start(adapter.build_start_cmd(inst), inst.dir)
                except RuntimeError as e:                  # 已在运行等场景：友好返回
                    return {"ok": False, "message": str(e)}
                return {"ok": True, "ws": f"/ws/login/{instance_id}"}
            self.w.reg.transition(instance_id, State.CONFIGURED)
            return {"ok": True, "ws": None}
