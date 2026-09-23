"""溯洄 Dice!：C++ 骰娘（独立程序，骰子端），经 OneBot 连登录端

部署要点（已对上游核实）：
- 主程序是 C++ 编译出的 Dice 可执行文件；GitHub release 只发行各平台 QQ 协议原生模块
  （w4123.Dice.<platform>.dll / w4123.Dice-<ver>.zip 里也只有 dll），主程序不在
  GitHub 发行（README 说预编译二进制在 Actions）→ manifest 走 manual，离线上传程序包。
- Dice! 自身不登录 QQ：它支持 OneBot 协议，由独立登录端（NapCat / SnowLuma / LLBot）
  接入 QQ，骰子端再连过去，故不再写 AutoLogin.yml / config.txt。
- OneBot 两端地址与 token 必须一致；Dice! 侧的连接配置格式未在本仓库实现，
  这里先给出可直接照抄的指引（确认其配置文件后可改为自动写入）。
"""
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult


class ShikiAdapter(BaseAdapter):
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]

    def configure_login(self, instance, credentials) -> dict:
        # QQ 登录由独立登录端完成（OneBot），Dice! 自身不登录
        return {"needs_login": False}

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        forward = direction != "reverse"
        port = addr.split(":")[-1].rstrip("/")
        if forward:
            guide = (f"正向 WS：Dice! 主动连接登录端\n"
                     f"  Dice! 侧填：ws://{addr}/\n"
                     f"  登录端侧：开启正向 WS 监听 {addr}，access token 用下面的 Token")
        else:
            guide = (f"反向 WS：登录端主动连 Dice!\n"
                     f"  Dice! 侧：监听端口 {port}\n"
                     f"  登录端侧填：ws://<Dice!所在IP>:{port}/")
        return WriteResult(
            ok=True,
            manual=f"{guide}\n  Token：{token}\n"
                   f"Dice! 的 OneBot 连接配置暂请在程序侧按上面填写（两端务必一致）。")

    def health_check(self, instance, is_alive=False) -> dict:
        return {"alive": is_alive, "conn": "ok" if is_alive else "down"}
