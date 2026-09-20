"""海豹：双形态（1.x 单文件 dice.yaml / 0.99.x 分文件 serve.yaml）+ 端点读写"""
from pathlib import Path

import yaml

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import write_atomic


class SealDiceAdapter(BaseAdapter):
    def _endpoints_file(self, instance) -> Path:
        """终检确认：imSession 在 serve.yaml 顶层（0.99.14 实测）；1.x 兼容单文件。"""
        base = Path(instance.dir)
        dy = base / "data" / "dice.yaml"
        y = yaml.safe_load(dy.read_text("utf-8")) if dy.exists() else {}
        if "imSession" in y:
            return dy
        data_dir = (y.get("diceConfigs") or [{}])[0].get("dataDir", "data/default")
        return base / data_dir / "serve.yaml"

    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"]),
                f"--address=127.0.0.1:{instance.port}"]       # 多开必须改端口

    def configure_login(self, instance, credentials) -> dict:
        return {"needs_login": False}                          # 登录端独立部署

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        path = self._endpoints_file(instance)
        doc = yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}
        eps = doc.setdefault("imSession", {}).setdefault("endPoints", [])
        forward = direction != "reverse"
        target = f"ws://{addr}" if forward else ""
        # 反向时 reverseAddr 是海豹自己的监听地址，需带 /ws 后缀
        reverse_addr = "" if forward else (
            addr if addr.rstrip("/").endswith("/ws") else f"{addr.rstrip('/')}/ws")
        entry = {"baseInfo": {"id": instance.id, "state": 0, "platform": "QQ",
                              "protocolType": "onebot", "enable": True,
                              "isPublic": False},
                 "adapter": {"isReverse": not forward,
                             "connectUrl": target,
                             "reverseAddr": reverse_addr,
                             "accessToken": token}}
        for ep in eps:                                          # 端点查重：命中即改
            ad = ep.get("adapter", {})
            if (ad.get("connectUrl") or "") == target and target or \
               (ad.get("reverseAddr") or "") == reverse_addr and reverse_addr:
                ep["adapter"] = entry["adapter"]; break
        else:
            eps.append(entry)
        write_atomic(path, yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode())
        # 正向无 /ws 后缀；反向需 /ws
        return WriteResult(ok=True, path=str(path),
                           manual=f"已写入 {path}。海豹需重启后生效"
                                  f"（尚未启动则下一步启动即生效）。")

    def health_check(self, instance, is_alive=False) -> dict:
        path = self._endpoints_file(instance)
        if not path.exists():
            return {"alive": is_alive, "conn": "none"}
        eps = (yaml.safe_load(path.read_text("utf-8")) or {}).get(
            "imSession", {}).get("endPoints", [])
        if not eps:
            return {"alive": is_alive, "conn": "none"}
        # 多端点时只认自己写入的那条（baseInfo.id 即实例 id），否则退回最后一条
        mine = [ep for ep in eps
                if ep.get("baseInfo", {}).get("id") == getattr(instance, "id", None)]
        state = (mine or eps[-1:])[0].get("baseInfo", {}).get("state", 0)
        conn = "ok" if state == 1 else ("down" if state in (0, 3) else "none")
        return {"alive": is_alive, "conn": conn}               # 0断开1已连接2连接中3失败
