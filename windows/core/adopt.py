"""端口→PID 接管工具：让面板在句柄失效（面板重启 / launcher reparent）后，
仍能按「监听端口」找回真实进程并接管，而不是误判死亡或重复拉起。

背景：llbot 等 launcher+worker 结构的程序，面板只持有 launcher 的 Popen 句柄，
真正监听端口的是 worker。面板重启 / launcher 重启后句柄失效，但 worker 仍在跑——
此前总览误判「已停止」，自动恢复又会因句柄失效而再拉一个重复的实例。

这里提供三件套：
- find_pid_on_port：某 TCP 端口当前被哪个 pid 监听（psutil 优先，Linux rootless 回退 /proc）
- pid_alive：pid 是否仍然存活
- kill_process_tree：按 pid 杀整棵进程树（向上找到 launcher 作为根，向下杀全部子进程），
  用于接管外部进程后正确停止（只杀 worker 会被 launcher 重新拉起）

平台适配：
  psutil.net_connections / Process.children / wait_procs 均跨平台，Windows 同样可用。
  _conn_listen_pid_procfs 的 /proc 回退是 Linux 专属，Windows 直接返回 None
  （psutil 在 Windows 同用户场景已能覆盖连接查询）。
"""
import os
import time

# psutil 在 probe() 等处已作为依赖使用，这里同样依赖它；仅在调用时 import，
# 避免在极早期 import 期把异常放大成启动失败。
try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - 依赖缺失时由调用方兜底
    psutil = None


def _conn_listen_pid(port: int) -> int | None:
    """通过 psutil 列出全部 TCP 连接，取 LISTEN 且本地端口匹配者 pid。

    需要读取其他进程的 pid 信息：同用户或 root 下可见（面板与子进程同用户/同 root，
    生产环境满足）。权限不足时 psutil 会把 pid 记为 None，此时由下方 /proc 回退补位。"""
    if psutil is None:
        return None
    try:
        for c in psutil.net_connections(kind="tcp"):
            if (getattr(c, "status", None) == psutil.CONN_LISTEN
                    and c.laddr is not None and c.laddr.port == port
                    and c.pid):
                return int(c.pid)
    except Exception:
        return None
    return None


def _conn_listen_pid_procfs(port: int) -> int | None:
    """rootless 回退（Linux）：解析 /proc/net/tcp[tcp6] 的 LISTEN 行定位 inode，
    再遍历 /proc/<pid>/fd 找出持有该 socket inode 的 pid。

    读取 /proc/<pid>/fd 的 readlink 对任意 uid 可见（只暴露 inode号），因此即便
    非 root 也能在面板与子进程同用户的场景命中。非 Linux 平台直接返回 None
    （Windows 上 psutil 已能覆盖同用户进程的连接查询）。"""
    if os.name != "posix":
        return None
    try:
        hexport = f"{int(port):04X}"
        inodes: set[str] = set()
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(path) as f:
                    next(f, None)
                    for line in f:
                        parts = line.split()
                        if len(parts) < 10:
                            continue
                        local, state, inode = parts[1], parts[3], parts[9]
                        if state != "0A":          # 0A = TCP_LISTEN
                            continue
                        lp = local.split(":")[1] if ":" in local else ""
                        if lp.upper() == hexport:
                            inodes.add(inode)
            except OSError:
                continue
        if not inodes:
            return None
        import glob
        for fdpath in glob.glob("/proc/[0-9]*/fd/*"):
            try:
                target = os.readlink(fdpath)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                return int(fdpath.split("/")[2])
    except Exception:
        return None
    return None


def find_pid_on_port(port: int) -> int | None:
    """返回当前正监听 TCP 端口 port 的进程 pid（无则 None）。异常安全。"""
    if not isinstance(port, int) or port <= 0:
        return None
    pid = _conn_listen_pid(port)
    if pid:
        return pid
    return _conn_listen_pid_procfs(port)


def pid_alive(pid: int) -> bool:
    """pid 是否仍然存活（异常时保守返回 False，绝不误判为存活）。

    僵尸进程已实质死亡（仅待父进程回收），不应算作存活——否则 re-adopt 接管后
    进程退出的瞬间会被 is_alive 误判为仍在跑。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if psutil is None:
        try:
            # 最后兜底：向进程发 0 信号探测（Windows/Unix 通用）
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False
    try:
        p = psutil.Process(pid)
        if not p.is_running():
            return False
        try:
            return p.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return True
    except Exception:
        return False


def kill_process_tree(pid: int, timeout: float = 10.0) -> None:
    """杀掉 pid 及其整棵进程树：向上找到真正的根（launcher），向下杀全部子进程。

    用于 re-adopt 后接管外部进程时的停止——launcher(父) + worker(端口占用者) 一并退出，
    避免只杀 worker 被 launcher 重新拉起。绝不杀 pid 1 或面板自身。

    跨平台：psutil.Process.children(recursive=True) 与 terminate()/kill() 在
    Windows 同样可用，无需平台分支。"""
    if psutil is None:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        return
    my_pid = os.getpid()
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return
    # 向上找根：端口占用者(worker)的父进程是 launcher，其上有 init/面板，到此为止。
    root = proc
    cur = proc
    for _ in range(6):                      # 限制上探深度，避免误入无关祖先
        par = cur.parent()
        if par is None or par.pid in (1, my_pid) or par.pid == cur.pid:
            break
        cur = par
        root = cur
    try:
        victims = root.children(recursive=True)
    except psutil.Error:
        victims = []
    victims.append(root)
    # 先 SIGTERM，留窗口自然退出，再 SIGKILL 犟留（子→父顺序，降低 reparent 抖动）
    for v in victims:
        try:
            v.terminate()
        except psutil.Error:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(v.is_running() for v in victims):
            break
        time.sleep(0.2)
    for v in victims:
        try:
            if v.is_running():
                v.kill()
        except psutil.Error:
            pass
    # 回收残留僵尸（避免 is_alive 误判），忽略已无主/已回收的竞态
    try:
        psutil.wait_procs(victims, timeout=3)
    except Exception:
        pass
