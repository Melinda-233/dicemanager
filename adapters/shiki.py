"""溯洄 Dice!：整合包 + 版本特征文件识别（AutoLogin.yml / config.txt）"""
import yaml
from pathlib import Path
from adapters.base import BaseAdapter, WriteResult
from core.atomicio import write_atomic

class ShikiAdapter(BaseAdapter):
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]

    def configure_login(self, instance, credentials) -> dict:
        if not credentials.get("qq") or not credentials.get("password"):
            return {"conflict": "请填写 QQ 账号与密码"}     # 缺参直接拦截，不再 KeyError 500
        proto = credentials.get("protocol", "ANDROID_PAD")
        if proto not in self.m.get("recommended_protocols", ["ANDROID_PAD", "ANDROID_WATCH"]) \
                and not credentials.get("confirm_protocol"):
            return {"conflict": f"协议 {proto} 非推荐值（PAD/WATCH），确认请带 confirm_protocol=true"}
        base = Path(instance.dir)
        new = base / "config" / "Console" / "AutoLogin.yml"
        if new.parent.exists():                                # 新版
            write_atomic(new, yaml.safe_dump({"accounts": [{
                "account": credentials["qq"],
                "password": {"kind": "plain", "value": credentials["password"]},
                "configuration": {"protocol": proto}}]},
                allow_unicode=True, sort_keys=False).encode())
        else:                                                  # 旧版 config.txt
            write_atomic(base / "config.txt",
                f"[Login]\n账号={credentials['qq']}\n密码={credentials['password']}\n协议={proto}\n".encode())
        return {"ok": True, "protocol": proto}

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        return WriteResult(ok=True)     # 整合包自包含，无外部 WS 配置

    def health_check(self, instance, is_alive=False) -> dict:
        return {"alive": is_alive, "conn": "ok" if is_alive else "down"}