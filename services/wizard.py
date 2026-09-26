"""五步向导状态机（断点续跑 + 同名冲突弹窗 + 后端统一构建启动命令）"""
import uuid
from pathlib import Path

from core.locks import instance_lock
from core.registry import State


class Wizard:
    def __init__(self, registry, adapter_registry, ports, processes, log_dir):
        self.reg = registry; self.adapters = adapter_registry
        self.ports = ports; self.pm = processes; self.log_dir = Path(log_dir)
        self._adapter_cache: dict = {}     # 与 ctx.get_adapter 同语义：实例复用、勿存请求态

    def get_adapter(self, name: str):
        if name not in self._adapter_cache:
            manifest, cls = self.adapters[name]
            self._adapter_cache[name] = cls(manifest)
        return self._adapter_cache[name]

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

    def next_step(self, instance_id: str) -> int:
        """断点续跑：按状态机给出该实例下一步该执行哪一步（供前端「继续」使用）。"""
        st = self.reg.get(instance_id).state
        if st in (State.UNDEPLOYED.value, State.DEPLOYING.value): return 2
        if st == State.AWAIT_LOGIN.value: return 3
        return 5                                  # CONFIGURED / RUNNING → 直接启动

    def _login_ref_info(self, inst):
        """关联的登录端实例的 (ob11 端口, 互联 token)；没关联返回 (None, None)，
        已关联但实例被删返回 ("dangling", None)——调用方须明确报错而不是静默换 token。"""
        if not inst.login_ref: return None, None
        try:
            li = self.reg.get(inst.login_ref)
            return (li.allocated_ports or {}).get("ob11"), li.conn_token
        except KeyError:
            return "dangling", None

    def _refresh_account(self, instance_id: str, inst, adapter) -> None:
        """扫码/账号登录后才拿得到 QQ 号：配置文件优先，其次日志锚点。"""
        if inst.qq: return
        qq = adapter.detect_account(inst)
        if not qq and hasattr(adapter, "account_from_logs"):
            qq = adapter.account_from_logs(self.pm.get(instance_id).ring)
        if qq: self.reg.update(instance_id, qq=qq)

    def _to_running(self, instance_id: str) -> None:
        """进入 RUNNING；断点续跑时可能已是 RUNNING（进程被外部杀掉后重启），
        状态机不允许 RUNNING→RUNNING，这里幂等处理而不是让 step5 500。"""
        try:
            self.reg.transition(instance_id, State.RUNNING)
        except ValueError:
            pass

    def start_instance(self, instance_id: str) -> tuple[str | None, bool]:
        """启动公共路径：REST start/restart、备份恢复、向导 step5 三入口共用。

        首启一次性动作（LLBot --update 等）→ WebUI 开放（绑定修正 + ufw，尽力而为）
        → 拉起进程。返回 (webui_note, already_running)；进程已在运行时不重复拉起。
        prepare/expose 失败不阻断启动，但必须落实例日志（此前静默吞掉，排障无据）。
        """
        inst = self.reg.get(instance_id)
        adapter = self.get_adapter(inst.dice)
        proc = self.pm.get(instance_id)
        if proc.is_alive():
            self._to_running(instance_id)
            return None, True

        def runner(cmd, cwd, label):                # 输出汇入实例日志流
            self.pm.run_once(instance_id, cmd, cwd, label=label)

        try:
            if adapter.prepare_start(inst, runner):
                self.reg.update(instance_id, first_run_done=True)
                inst.first_run_done = True
        except Exception as e:
            proc.note(f"prepare_start 失败（不阻断启动）: {e}")
        try:
            note = adapter.expose_webui(inst)
        except Exception as e:
            proc.note(f"WebUI 开放失败（不阻断启动）: {e}")
            note = None
        # actual_port 不在此回读：启动瞬间进程还没打印端口（恒为 None）。
        # 由 ws_overview 周期从 ring 提取并回填（actual_port 变化才写盘）。
        proc.start(adapter.build_start_cmd(inst), inst.dir)
        self._to_running(instance_id)
        return note, False

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
                # 升级通道基线：部署时解析到的 release tag 落盘（base 不持有 registry）
                from adapters.base import deploy_version_of
                if ver := deploy_version_of(instance_id):
                    self.reg.update(instance_id, version=ver)
                if inst.warnings:          # 适配器只改副本，必须显式落盘（OlivaDice 缺件告警）
                    self.reg.update(instance_id, warnings=list(inst.warnings))
                self.reg.transition(instance_id, State.AWAIT_LOGIN)
                return {"result": "ok"}

            if step == 3:                                      # 登录（needs_login 以适配器为准）
                r = adapter.configure_login(inst, payload.get("credentials", {}))
                if r.get("conflict"):
                    return {"result": "conflict", "message": r["conflict"]}
                if payload.get("qq"):
                    self.reg.update(instance_id, qq=payload["qq"]); inst.qq = payload["qq"]
                self._refresh_account(instance_id, inst, adapter)   # 扫码后回读账号
                login_type = self.adapters[inst.dice][0].get("login_type", "none")
                needs_login = bool(r.get("needs_login",
                                         login_type in ("qrcode", "account")))
                if not needs_login:
                    self.reg.transition(instance_id, State.CONFIGURED)
                return {"result": "ok", "needs_login": needs_login, **r}

            if step == 4:                                      # 互联配置写入 + 预览
                self._refresh_account(instance_id, inst, adapter)  # 可能刚扫码成功
                login_port, login_token = self._login_ref_info(inst)
                if login_port == "dangling":
                    # 关联的登录端已删：若静默继续会自动生成新 token，与登录端残留
                    # 配置不一致 → 两端连不上且难排查。明确报错让用户先重新关联。
                    return {"result": "error",
                            "message": f"关联的登录端 {inst.login_ref} 已不存在"
                                       f"（可能已删除），请先在总览重新关联登录端"}
                direction = payload.get("direction") or inst.conn_direction or "forward"
                # 默认地址：自己有 ob11 端口（登录端作服务端）用自己，否则连登录端的端口
                default_port = (inst.allocated_ports or {}).get("ob11") or login_port or 3001
                addr = payload.get("addr") or inst.conn_addr or f"127.0.0.1:{default_port}"
                # 两端 token 必须一致：登录端先生成，骰子端经 login_ref 继承同一个
                token = (payload.get("token") or inst.conn_token or login_token
                         or adapter.gen_token())
                wr = adapter.write_conn_config(inst, payload.get("mode", "ob11"),
                                               direction, addr, token)
                self.reg.update(instance_id, conn_token=token, conn_addr=addr,
                                conn_direction=direction)
                if not wr.ok:
                    return {"result": "error", "message": wr.manual or "互联配置写入失败"}
                kind = "正向" if direction != "reverse" else "反向"
                return {"result": "ok",
                        "preview": f"{kind} WS → {addr}\nToken: {token}",
                        "token": token, "path": wr.path, "manual": wr.manual}

            if step == 5:                                      # 启动（与 REST start 同一公共路径）
                try:
                    note, already = self.start_instance(instance_id)
                except RuntimeError as e:                      # 竞态下刚好被启动：友好返回而非 500
                    return {"result": "error", "message": str(e)}
                if already:
                    return {"result": "ok", "already_running": True}
                return {"result": "ok", "webui_note": note}
        return {"result": "unknown_step"}
