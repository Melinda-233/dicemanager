"""[已弃用] 首次启动自设密码对话框（原生 Windows GUI，不依赖 tkinter）。

⚠️ 本模块当前已不再被 launcher 调用——首次启动密码设置流程改为走 WebUI：
auth.json 不存在时 api/auth.py 的 Auth() 进入「未初始化」态，前端访问
/api/needs-setup 探测后切到「设置管理密码」界面，POST /api/setup 完成设置。
原因：早期方案在精简版 Windows（无 GUI 子系统）下原生弹窗可能弹不出来，
用户体验不可控；走 WebUI 流程跨平台一致、可调试、文本可定制。

本文件保留以备未来场景重新启用（如纯命令行无浏览器的服务器模式）。

历史实现：用 win32gui + win32con 直接 CreateWindowEx + Edit/Button 控件，
仅依赖系统 DLL；密码以 ES_PASSWORD 风格显示圆点，明文不落盘，
仅返回给调用方由 auth.py 写入 PBKDF2 哈希。

入口：show_password_dialog() → 返回用户设置的明文密码（str）；
用户关闭窗口不设密码 → 返回 None。
"""
from __future__ import annotations

import win32api
import win32con
import win32gui


class _PasswordDialog:
    """两个 Edit（密码+确认）+ 一个确认按钮的原生窗口。

    用 win32gui.PumpMessages 跑消息循环；按钮点击校验 → 一致则存 self.result 并销毁窗口。
    """

    def __init__(self) -> None:
        self.result: str | None = None
        self.hwnd: int | None = None
        self.h_edit1: int | None = None  # 密码
        self.h_edit2: int | None = None  # 确认
        self._hfont: int | None = None

    # ---- Win32 消息回调 ----
    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> None:
        if msg == win32con.WM_COMMAND:
            ctrl_id = win32api.LOWORD(wparam)
            notif = win32api.HIWORD(wparam)
            if ctrl_id == 104 and notif == win32con.BN_CLICKED:  # 确认按钮
                self._on_confirm(hwnd)
                return
            if ctrl_id == 105 and notif == win32con.BN_CLICKED:  # 取消按钮
                win32gui.DestroyWindow(hwnd)
                return
            # 在 Edit 上按 Enter 触发确认（BS_DEFPUSHBUTTON 已让 Enter 默认走确认按钮）
        elif msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
        elif msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)

    def _on_confirm(self, hwnd: int) -> None:
        pwd1 = win32gui.GetWindowText(self.h_edit1)
        pwd2 = win32gui.GetWindowText(self.h_edit2)
        if not pwd1:
            win32gui.MessageBox(hwnd, "密码不能为空", "DiceManager 提示",
                                win32con.MB_ICONWARNING)
            return
        if len(pwd1) < 6:
            win32gui.MessageBox(hwnd, "密码至少 6 位", "DiceManager 提示",
                                win32con.MB_ICONWARNING)
            return
        if pwd1 != pwd2:
            win32gui.MessageBox(hwnd, "两次输入不一致，请重新输入",
                                "DiceManager 提示", win32con.MB_ICONWARNING)
            win32gui.SetWindowText(self.h_edit1, "")
            win32gui.SetWindowText(self.h_edit2, "")
            win32gui.SetFocus(self.h_edit1)
            return
        self.result = pwd1
        win32gui.DestroyWindow(hwnd)

    def _apply_font(self, hwnd: int) -> None:
        """统一用 Segoe UI 16px，否则控件用 System 字体看着像 Windows 95。"""
        lf = win32gui.LOGFONT()
        lf.lfHeight = -16  # 负值 = 字符高度（px）
        lf.lfWeight = win32con.FW_NORMAL
        lf.lfCharSet = win32con.DEFAULT_CHARSET
        lf.lfFaceName = "Segoe UI"
        self._hfont = win32gui.CreateFontIndirect(lf)
        for ctrl in (self.h_edit1, self.h_edit2):
            if ctrl:
                win32gui.SendMessage(ctrl, win32con.WM_SETFONT, self._hfont, 1)

    def show(self) -> str | None:
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_BTNFACE + 1
        wc.lpszClassName = "DiceManagerPasswordDialog"
        class_atom = win32gui.RegisterClass(wc)

        # 主窗口（不可调整大小：WS_OVERLAPPED | WS_SYSMENU 仅有关闭按钮）
        self.hwnd = win32gui.CreateWindowEx(
            0, class_atom, "DiceManager — 首次设置管理密码",
            win32con.WS_OVERLAPPED | win32con.WS_SYSMENU,
            win32con.CW_USEDEFAULT, win32con.CW_USEDEFAULT,
            380, 230, 0, 0, wc.hInstance, None,
        )

        hinst = wc.hInstance
        win32gui.CreateWindowEx(0, "STATIC", "请设置管理密码（≥6 位）：",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
            20, 20, 320, 20, self.hwnd, 100, hinst, None)
        self.h_edit1 = win32gui.CreateWindowEx(0, "EDIT", "",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_BORDER
            | win32con.ES_PASSWORD | win32con.ES_AUTOHSCROLL,
            20, 45, 320, 26, self.hwnd, 101, hinst, None)
        win32gui.CreateWindowEx(0, "STATIC", "确认密码：",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_LEFT,
            20, 80, 320, 20, self.hwnd, 102, hinst, None)
        self.h_edit2 = win32gui.CreateWindowEx(0, "EDIT", "",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_BORDER
            | win32con.ES_PASSWORD | win32con.ES_AUTOHSCROLL,
            20, 105, 320, 26, self.hwnd, 103, hinst, None)
        win32gui.CreateWindowEx(0, "BUTTON", "确认",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_DEFPUSHBUTTON,
            175, 150, 80, 32, self.hwnd, 104, hinst, None)
        win32gui.CreateWindowEx(0, "BUTTON", "退出",
            win32con.WS_CHILD | win32con.WS_VISIBLE,
            260, 150, 80, 32, self.hwnd, 105, hinst, None)

        self._apply_font(self.hwnd)

        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNORMAL)
        win32gui.UpdateWindow(self.hwnd)
        win32gui.SetFocus(self.h_edit1)

        win32gui.PumpMessages()
        return self.result


def show_password_dialog() -> str | None:
    """首次启动自设密码入口。返回明文密码；用户点退出或不设 → 返回 None。"""
    return _PasswordDialog().show()


if __name__ == "__main__":
    # 直接运行此文件可单测对话框
    pwd = show_password_dialog()
    print(f"got password length={len(pwd) if pwd else 'None'}")
