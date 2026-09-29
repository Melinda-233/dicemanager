"""系统托盘图标（任务栏右下角）+ 右键菜单。

用 Pillow 动态生成骰子图标（无需 .ico 资源文件），pystray 创建托盘。
主线程跑 icon.run() 阻塞，uvicorn 在 launcher 拉起的子线程里跑。

菜单：
  打开面板（default，双击托盘也触发）→ webbrowser.open(8765)
  ───
  重启服务 → 停托盘 + spawn 新进程 + os._exit
  退出    → 停托盘 + os._exit

退出用 os._exit(0) 强制结束整个进程（含 uvicorn 子线程），避免
uvicorn 优雅停止等待连接关闭导致托盘已消失但进程未退出的诡异状态。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from typing import Callable

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"


def _make_icon() -> Image.Image:
    """64×64 透明背景 + 白色圆角方块 + 6 个黑点（骰子 6 面）。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 骰子主体：白色圆角方块，带浅灰描边
    d.rounded_rectangle([6, 6, 58, 58], radius=12,
                        fill=(255, 255, 255), outline=(80, 80, 80), width=2)
    # 6 个黑点（两列三行布局）
    dots = [(20, 20), (44, 20), (20, 32), (44, 32), (20, 44), (44, 44)]
    for x, y in dots:
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(40, 40, 40))
    return img


def _on_open(icon: Icon, item: MenuItem) -> None:
    """打开面板（默认操作，双击托盘图标也触发）。"""
    try:
        webbrowser.open(URL)
    except Exception:
        pass  # 浏览器启动失败不阻断


def _on_restart(icon: Icon, item: MenuItem) -> None:
    """重启：spawn 新进程（同一 exe）后立即退出当前进程。"""
    icon.stop()  # 停止托盘消息循环
    try:
        # --onefile 模式下 sys.executable 即 exe 本身；spawn 后立即退出
        subprocess.Popen([sys.executable])
    except Exception as e:
        # 兜底：spawn 失败也允许用户从托盘退出（消息框提示）
        try:
            import win32gui, win32con
            win32gui.MessageBox(0, f"重启失败：{e}", "DiceManager",
                                win32con.MB_ICONERROR)
        except Exception:
            pass
        return
    os._exit(0)


def _on_exit(icon: Icon, item: MenuItem) -> None:
    """退出：停托盘 + 强制结束进程（含 uvicorn 子线程）。"""
    icon.stop()
    os._exit(0)


def _build_menu() -> Menu:
    return Menu(
        MenuItem("打开面板", _on_open, default=True),
        Menu.SEPARATOR,
        MenuItem("重启服务", _on_restart),
        MenuItem("退出", _on_exit),
    )


def run_tray(on_ready: Callable[[], None] | None = None) -> None:
    """启动托盘消息循环（阻塞主线程）。

    on_ready：托盘启动前的回调（如需在 uvicorn listen 后通知，建议改用
    pystray 的 visible 属性或自定义事件；此处保留接口但 pystray 当前
    版本无 setup，仅用 on_ready() 同步调用一次后 run）。
    """
    icon = Icon(
        "dicemanager",
        _make_icon(),
        "DiceManager",
        menu=_build_menu(),
    )
    if on_ready:
        on_ready()  # 同步触发后再进 run 阻塞
    icon.run()


if __name__ == "__main__":
    # 单测：直接弹托盘
    print("托盘测试：右键托盘可见菜单，双击打开浏览器")
    run_tray()
