"""海豹：双形态（1.x 单文件 dice.yaml / 0.99.x 分文件 serve.yaml）+ 端点读写"""
import datetime
from pathlib import Path

import yaml

from adapters.base import BaseAdapter, WriteResult
from adapters.base import link_id as base_link_id
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

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        path = self._endpoints_file(instance)
        doc = yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}
        eps = doc.setdefault("imSession", {}).setdefault("endPoints", [])
        forward = direction != "reverse"
        is_milky = (mode == "milky")
        proto = "milky" if is_milky else "onebot"
        # 每条 骰子端↔登录端 关联一个全局唯一端点 id（一连多时一个海豹挂多个端点互不覆盖）
        ep_id = link_id or instance.id
        # 正向：本端连登录端；反向：登录端连本端
        if is_milky:
            # Milky 基址为 http://host:port（HTTP API + /event WS 事件流），非 ws://
            target = f"http://{addr}" if forward else ""
            reverse_addr = "" if forward else (
                addr if addr.rstrip("/").endswith("/event")
                else f"{addr.rstrip('/')}/event")
        else:
            target = f"ws://{addr}" if forward else ""
            # 反向时 reverseAddr 是海豹自己的监听地址，需带 /ws 后缀
            reverse_addr = "" if forward else (
                addr if addr.rstrip("/").endswith("/ws") else f"{addr.rstrip('/')}/ws")
        entry = {"baseInfo": {"id": ep_id, "state": 0, "platform": "QQ",
                              "protocolType": proto, "enable": True,
                              "isPublic": False},
                 "adapter": {"isReverse": not forward,
                             "connectUrl": target,
                             "reverseAddr": reverse_addr,
                             "accessToken": token}}
        own = None
        for ep in eps:
            # 先按 baseInfo.id 认领自己的端点（改地址也原地更新），
            # 再退回按地址查重（兼容旧版本写入的端点）
            if ep.get("baseInfo", {}).get("id") == ep_id:
                own = ep
                break
            ad = ep.get("adapter", {})
            if (ad.get("connectUrl") or "") == target and target or \
               (ad.get("reverseAddr") or "") == reverse_addr and reverse_addr:
                own = ep
                break
        if own is not None:
            own["baseInfo"]["id"] = ep_id
            own["adapter"] = entry["adapter"]
            own["baseInfo"]["enable"] = True
        else:
            eps.append(entry)
        #  reconcile：只保留本实例 links 声明的端点，其余同协议端点停用（清理旧机备份带回 /
        #  已解除关联的残留端点）。一连多时保留全部声明的端点，不互相覆盖。
        links = getattr(instance, "links", None) or []
        valid_ids = {base_link_id(lk.get("login_ref"), lk.get("account_qq")) for lk in links} \
            if links else {ep_id}
        for ep in eps:
            bi = ep.get("baseInfo", {})
            if bi.get("protocolType", "onebot") == proto and bi.get("id") not in valid_ids:
                bi["enable"] = False
        doc = _stringify_datetimes(doc)
        write_atomic(path, yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode())
        kind = "正向" if forward else "反向"
        suffix = "（Milky 基址 http://...，非 WS）" if is_milky else ""
        return WriteResult(ok=True, path=str(path),
                           manual=f"已写入 {path}（{kind} {proto}{suffix}）。"
                                  f"海豹需重启后生效（尚未启动则下一步启动即生效）。")

    @staticmethod
    def _owned_ids(instance) -> set:
        """本实例在 serve.yaml/dice.yaml 里拥有的端点 id 集合。

        多关联（多连一/一连多）后端点 id 取 link_id（login_ref|account_qq）；
        兼容改造前/无 links 的实例：并上实例 id（旧版写入的端点以实例 id 命名）。"""
        links = getattr(instance, "links", None) or []
        ids = {base_link_id(lk.get("login_ref"), lk.get("account_qq"))
               for lk in links if lk.get("login_ref")}
        iid = getattr(instance, "id", None)
        if iid:
            ids.add(iid)
        return ids

    def health_check(self, instance, is_alive=False) -> dict:
        path = self._endpoints_file(instance)
        if not path.exists():
            return {"alive": is_alive, "conn": "none"}
        eps = (yaml.safe_load(path.read_text("utf-8")) or {}).get(
            "imSession", {}).get("endPoints", [])
        if not eps:
            return {"alive": is_alive, "conn": "none"}
        # 多关联时本实例拥有多个端点（各按 link_id 命名）：任一已连接即视为连通，
        # 全断开/失败按失败报；无本实例端点（理论上不应发生）退回最后一条兜底。
        owned = self._owned_ids(instance)
        mine = [ep for ep in eps if ep.get("baseInfo", {}).get("id") in owned]
        states = {ep.get("baseInfo", {}).get("state", 0) for ep in (mine or eps[-1:])}
        conn = "ok" if 1 in states else ("down" if states & {0, 3} else "none")
        return {"alive": is_alive, "conn": conn}               # 0断开1已连接2连接中3失败

    def diagnose_conn(self, instance) -> list[dict]:
        """专属诊断：serve.yaml/dice.yaml 端点存在性与连接状态（state: 0断1连3失败）。

        多关联（一连多）时逐条端点给结论（端点 id 为 link_id：login_ref|account_qq）。"""
        items: list[dict] = []
        path = self._endpoints_file(instance)
        if not path.exists():
            return [{"ok": False, "step": "config",
                     "detail": f"互联配置文件不存在（{path}），请先在向导/总览重写互联配置"}]
        try:
            eps = (yaml.safe_load(path.read_text("utf-8")) or {}).get(
                "imSession", {}).get("endPoints", [])
        except Exception as e:
            return [{"ok": False, "step": "config", "detail": f"配置解析失败: {e}"}]
        owned = self._owned_ids(instance)
        mine = [ep for ep in eps if ep.get("baseInfo", {}).get("id") in owned]
        if not mine:
            items.append({"ok": False, "step": "config",
                          "detail": "配置中没有本实例的 OneBot 端点，请重写互联配置"})
            return items
        state_text = {0: "端点未连接（海豹侧显示断开）",
                      1: "海豹侧已建立 WebSocket 连接",
                      2: "连接中（等待对端）",
                      3: "连接失败（地址/token 与登录端不一致？）"}
        for ep in mine:
            ad = ep.get("adapter", {})
            state = ep.get("baseInfo", {}).get("state", 0)
            eid = str(ep.get("baseInfo", {}).get("id") or "")
            acct = eid.split("|", 1)[1] if "|" in eid else ""
            who = f"（账号 {acct}）" if acct else ""
            items.append({"ok": True, "step": "config",
                          "detail": f"端点{who}已配置：{'反向' if ad.get('isReverse') else '正向'} "
                                    f"{ad.get('connectUrl') or ad.get('reverseAddr') or '(空)'}"})
            items.append({"ok": state == 1, "step": "state",
                          "detail": f"{state_text.get(state, f'未知状态 {state}')}{who}"})
        return items
