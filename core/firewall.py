"""防火墙端口放行（尽力而为，绝不阻断主流程）

设计约束：
- 只处理 ufw（Ubuntu 事实标准）与 firewalld（CentOS/openEuler 事实标准）；
  未安装 / 未启用 / 非 root 一律静默跳过。
- iptables 直接改表风险高（易与 Docker/宝塔等已有规则打架），不做。
- 云厂商安全组（阿里云等）在宿主机之外，程序无能为力，提示用户自行放行。
"""
import shutil
import subprocess


def _ufw(port: int) -> str | None:
    ufw = shutil.which("ufw")
    if not ufw:
        return None
    st = subprocess.run([ufw, "status"], capture_output=True, text=True, timeout=10)
    if "Status: active" not in (st.stdout or ""):
        return None                     # 未启用：不添加死规则，也不打扰用户
    r = subprocess.run([ufw, "allow", f"{port}/tcp"],
                       capture_output=True, text=True, timeout=20)
    if r.returncode == 0:
        return f"已用 ufw 放行 {port}/tcp（云服务器还需在厂商安全组放行该端口）"
    return None


def _firewalld(port: int) -> str | None:
    fc = shutil.which("firewall-cmd")
    if not fc:
        return None
    st = subprocess.run([fc, "--state"], capture_output=True, text=True, timeout=10)
    if st.returncode != 0:
        return None                     # 未运行
    r = subprocess.run([fc, "--add-port", f"{port}/tcp"],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return None                     # 已放行/失败：静默
    p = subprocess.run([fc, "--permanent", "--add-port", f"{port}/tcp"],
                       capture_output=True, text=True, timeout=20)
    if p.returncode == 0:               # 永久规则落盘后 reload 生效于下次（运行时规则已即时生效）
        subprocess.run([fc, "--reload"], capture_output=True, text=True, timeout=30)
    return f"已用 firewalld 放行 {port}/tcp（云服务器还需在厂商安全组放行该端口）"


def open_port(port: int | None) -> str | None:
    """ufw/firewalld 启用时放行 port/tcp。返回用户可读提示；无动作/失败返回 None。"""
    if not port:
        return None
    port = int(port)
    try:
        for fn in (_ufw, _firewalld):
            note = fn(port)
            if note:
                return note
    except (OSError, subprocess.SubprocessError):
        pass                            # 防火墙异常不阻断启动
    return None
