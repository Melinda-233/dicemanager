"""安装目录扫描：安装根下的目录与进程 vs 注册表比对。

游离目录的典型来源：管理器外的手工部署、删除链路失败的历史残留、程序自更新备份。
分类口径（与前端展示一一对应）：
  owned    —— 注册表实例的 dir，受管理
  orphan   —— 目录名匹配已知程序名（含 -N 序号后缀），但无实例记录 → 可清理
  external —— 其余目录（alist/containerd 等无关软件）→ 只展示，不提供删除
"""
import re
import time
from pathlib import Path


def classify_dir(child: Path, owned: dict, names: list[str]) -> str:
    """owned: {dir_path: {...实例摘要}}；names: manifests 里的程序名列表。"""
    if str(child) in owned:
        return "owned"
    if any(re.fullmatch(re.escape(n) + r"(-\d+)?", child.name) for n in names):
        return "orphan"
    return "external"


def scan_dirs(roots: list[str], owned: dict, names: list[str],
              skip: list[str] | None = None) -> list[dict]:
    """枚举安装根下的一级目录并标注归属/分类/mtime。skip: 管理器自身目录等。"""
    skipped = set(skip or [])
    result = []
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        for child in sorted(rp.iterdir()):
            if not child.is_dir() or str(child) in skipped:
                continue
            kind = classify_dir(child, owned, names)
            item = {"path": str(child), "root": root, "kind": kind,
                    "mtime": time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(child.stat().st_mtime))}
            if kind == "owned":
                item.update(owned[str(child)])
            result.append(item)
    return result


def _under(path: str, base: str) -> bool:
    """path 是否等于或位于 base 之下。必须兼容「恰好相等」：venv 进程的 cwd 是
    /opt/dicemanager（无尾斜杠），旧写法 base 强拼尾斜杠导致 startswith 失配。"""
    b = base.rstrip("/")
    return path == b or path.startswith(b + "/")


def match_program_dir(path: str, roots: list[str], names: list[str]) -> bool:
    """path（exe 或 cwd）是否位于安装根内、且其下某级目录名匹配已知程序名
    （含 -N 序号后缀，与 classify_dir 的 orphan 口径同源）。

    进程侧「可结束」的判定依据：进程名不可靠（二进制改名、解释器包装启动
    都很常见），落盘位置才是锚点。external 软件（alist 等）的目录不匹配
    → 返回 False，其进程只展示、不提供结束，与目录侧「避免误伤」一致。"""
    p = path.rstrip("/")
    for r in roots:
        rb = r.rstrip("/")
        if not (p == rb or p.startswith(rb + "/")):
            continue
        rel = p[len(rb):].lstrip("/")
        if any(seg and any(re.fullmatch(re.escape(n) + r"(-\d+)?", seg)
                           for n in names)
               for seg in rel.split("/")):
            return True
    return False


def scan_procs(roots: list[str], owned: dict, names: list[str],
               skip: list[str] | None = None) -> list[dict]:
    """找出 exe/cwd 落在安装根或实例目录下的进程，标注所属实例（无 → 游离）。

    names: manifests 程序名列表——owner=None 的进程仅当 exe/cwd 位于
    match_program_dir 命中的目录下才 killable（external 软件进程只展示）。
    skip: 管理器自身目录——其中的进程（如 venv 里的 api.app）不算游离。
    注意 psutil 的 exe 会解析符号链接（venv python → 系统解释器），目录归属
    必须同时看 exe 与 cwd。"""
    import psutil
    skipped = list(skip or [])
    out = []
    for p in psutil.process_iter(["pid", "exe", "cwd", "cmdline"]):
        try:
            i = p.info
            exe, cwd = i.get("exe") or "", i.get("cwd") or ""
            if any(_under(exe, s) or _under(cwd, s) for s in skipped):
                continue                       # 管理器自身进程，不纳入扫描
            owner = next((iid["id"] for d, iid in owned.items()
                          if _under(exe, d) or _under(cwd, d)), None)
            if owner is None and not any(_under(exe, r) or _under(cwd, r)
                                         for r in roots):
                continue                       # 与安装根毫无交集的进程不纳入
            killable = owner is None and (
                match_program_dir(exe, roots, names)
                or match_program_dir(cwd, roots, names))
            out.append({"pid": p.pid, "exe": exe, "cwd": cwd, "owner": owner,
                        "killable": killable,
                        "cmd": " ".join(i.get("cmdline") or [])[:200]})
        except Exception:                      # NoSuchProcess/AccessDenied/Zombie 竞态
            continue
    return out


def check_deletable(path: str, roots: list[str], protected_dirs,
                    names: list[str]) -> None:
    """orphan 目录删除守卫：三重校验，非法即抛 ValueError。

    protected_dirs 必须以 Path 与 Path 比较——曾因 `str(p) in {Path(...)}` 恒为
    False 把受管实例目录当 orphan 放行删除（2026-09-25 事故），此处锁死口径；
    protected_dirs 命中或为其子目录一律拒绝。"""
    p = Path(path).resolve()
    if not any(p.is_relative_to(Path(r).resolve()) and p != Path(r).resolve()
               for r in roots):
        raise ValueError(f"目录不在安装根下: {path}")
    prot = [Path(d).resolve() for d in protected_dirs]
    if any(p == d or p.is_relative_to(d) for d in prot):
        raise ValueError("目录属于受保护目录（实例或管理器），禁止在此删除")
    if classify_dir(p, {}, names) != "orphan":
        raise ValueError("目录不匹配任何程序命名，可能与管理工具无关，拒绝删除")
