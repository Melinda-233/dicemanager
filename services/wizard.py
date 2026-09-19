"""五步向导状态机（断点续跑 + 同名冲突弹窗 + 后端统一构建启动命令）"""
import uuid
from pathlib import Path
from core.locks import instance_lock
from core.registry import State

class Wizard:
    def __init__(self, registry, adapter_registry, ports, processes, log_dir):
        self.reg = registry; self.adapters = adapter_registry
        self.ports = ports; self.pm = processes; self.log_dir = Path(log_dir)

    def get_adapter(self, name: str):
        manifest, cls = self.adapters[name]
        return cls(manifest)

    def create_instance(self, dice, arch, login_ref=None, confirm_dir=False) -> str:
        if dice not in self.adapters:
            raise ValueError(f"未知程序: {dice}")
        manifest, _ = self.adapters[dice]
        iid = f"{dice}-{uuid.uuid4().hex[:8]}"
        roles = {r: manifest.get(f"{r}_default_port")
                 for r in ("webui", "ob11", "milky", "satori")}
        ports = self.ports.allocate_many(dice, {k: v for k, v in roles.items() if v}, owner=iid)
        root = Path(manifest["install_root"])
        d = root / dice
        if d.exists() and not confirm_dir:                     # 同名 → 序号文件夹
            n = 2
            while (root / f"{dice}-{n}").exists(): n += 1
            d = root / f"{dice}-{n}"
        self.reg.create(iid, dice=dice, arch=arch, dir_=str(d),
                        port=ports.get("webui", 0), allocated_ports=ports,
                        login_ref=login_ref)
        return iid

    def run_step(self, instance_id: str, step: int, payload: dict) -> dict:
        with instance_lock(instance_id):
            inst = self.reg.get(instance_id)
            adapter = self.get_adapter(inst.dice)

            if step == 1:
                return {"result": "ok", "ports": inst.allocated_ports}

            if step == 2:                                      # 部署（冲突 → 弹窗二选一）
                if inst.state not in (State.UNDEPLOYED.value, State.DEPLOYING.value):
                    return {"result": "ok"}                    # 断点续跑：已部署过
                if inst.state == State.UNDEPLOYED.value:
                    self.reg.transition(instance_id, State.DEPLOYING)
                if payload.get("use_existing") is False:       # 弹窗选「新建序号文件夹」
                    manifest, _ = self.adapters[inst.dice]
                    root = Path(manifest["install_root"])
                    n = 2
                    while (root / f"{inst.dice}-{n}").exists(): n += 1
                    self.reg.update(instance_id, dir=str(root / f"{inst.dice}-{n}"))
                    inst = self.reg.get(instance_id)
                result = adapter.deploy(inst)
                if result == "conflict":
                    return {"result": "conflict", "dir": inst.dir}
                self.reg.transition(instance_id, State.AWAIT_LOGIN)
                return {"result": "ok"}

            if step == 3:                                      # 登录（needs_login 以适配器为准）
                r = adapter.configure_login(inst, payload.get("credentials", {}))
                if r.get("conflict"):
                    return {"result": "conflict", "message": r["conflict"]}
                if payload.get("qq"):
                    self.reg.update(instance_id, qq=payload["qq"])
                login_type = self.adapters[inst.dice][0].get("login_type", "none")
                needs_login = bool(r.get("needs_login",
                                         login_type in ("qrcode", "account")))
                if not needs_login:
                    self.reg.transition(instance_id, State.CONFIGURED)
                return {"result": "ok", "needs_login": needs_login, **r}

            if step == 4:                                      # 互联配置写入 + 预览
                addr = payload.get("addr",
                    f"127.0.0.1:{inst.allocated_ports.get('ob11', 3001)}")
                wr = adapter.write_conn_config(inst, payload.get("mode", "ob11"),
                                               payload.get("direction", "forward"),
                                               addr, payload.get("token", ""))
                if wr.manual:
                    self.reg.update(instance_id, warnings=[wr.manual])
                    return {"result": "ok", "manual": wr.manual}
                return {"result": "ok", "preview":
                        f"{'正向' if payload.get('direction') == 'forward' else '反向'} WS → {addr}"}

            if step == 5:                                      # 启动（命令后端构建，安全）
                proc = self.pm.get(instance_id)
                if proc.is_alive():
                    self.reg.transition(instance_id, State.RUNNING)
                    return {"result": "ok", "already_running": True}
                cmd = adapter.build_start_cmd(inst)
                proc.start(cmd, inst.dir)
                actual = adapter.get_actual_port(proc.ring)
                self.reg.update(instance_id, actual_port=actual)
                self.reg.transition(instance_id, State.RUNNING)
                return {"result": "ok"}
        return {"result": "unknown_step"}
