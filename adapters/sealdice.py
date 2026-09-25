"""海豹：双形态（1.x 单文件 dice.yaml / 0.99.x 分文件 serve.yaml）+ 端点读写"""
import datetime
from pathlib import Path

import yaml

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import write_atomic


def _stringify_datetimes(obj):
    """pyyaml 会把 serve.yaml 里 RFC3339 时间戳（lastSavedTime 等）自动解析成
    datetime，safe_dump 写回时变成 "2026-09-25 18:17:38.182038+08:00"——丢了 'T'
    且纳秒精度降为微秒，海豹按 RFC3339 严格解析直接 panic（serve.yaml parse
    failed: cannot parse ... as "T"）。写回前把 datetime/date 恢复为 isoformat。"""
    if isinstance(obj, dict):
        return {k: _stringify_datetimes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_datetimes(v) for v in obj]
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    return obj


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
                f"--address=0.0.0.0:{instance.port}"]   # 多开必须改端口；0.0.0.0 才能从外网访问 UI

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
        own = None
        for ep in eps:
            # 先按 baseInfo.id 认领自己的端点（改地址也原地更新），
            # 再退回按地址查重（兼容旧版本写入的端点）
            if ep.get("baseInfo", {}).get("id") == instance.id:
                own = ep
                break
            ad = ep.get("adapter", {})
            if (ad.get("connectUrl") or "") == target and target or \
               (ad.get("reverseAddr") or "") == reverse_addr and reverse_addr:
                own = ep
                break
        if own is not None:
            own["baseInfo"]["id"] = instance.id
            own["adapter"] = entry["adapter"]
            own["baseInfo"]["enable"] = True
        else:
            eps.append(entry)
        # 备份恢复会把旧机器的互联端点原样带回来（地址指向旧机的登录端），
        # 一直拨号报错。以 baseInfo.id 认领本实例端点，其余 onebot 端点一律停用。
        for ep in eps:
            bi = ep.get("baseInfo", {})
            if bi.get("id") != instance.id and \
               bi.get("protocolType", "onebot") == "onebot" and bi.get("enable"):
                bi["enable"] = False
        doc = _stringify_datetimes(doc)
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
