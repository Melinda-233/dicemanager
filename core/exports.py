"""备份产物目录：手动导出 / 升级前快照 / 定时备份的统一落盘与回收。

背景（2026-09-26 评估发现）：升级通道每次升级会生成 `<dice>-<id>-preupgrade-<ts>.tar.gz`
并永久留在状态目录，定时备份另有 `backups/` 目录且只有它自己的 keep 份数滚动——
两侧目录与口径都不统一，长期会在小内存机器上堆出几百 MB，且没有任何回收入口。
这里统一成一个 `exports/` 目录 + 一套列表 / 单删 / 按天清理入口，
与 `core/packages.py` 的死缓存清理同构（先列后删、freed_mb 用真实字节算）。

命名约定（互不影响的关键）：
  升级前快照   `<dice>-<id>-preupgrade-<ts>.tar.gz`
  定时备份     `<dice>-<id>-sched-<scope>-<ts>.tar.gz`   ← 带 sched 标记，
               定时任务的 keep 滚动只认这个标记，不会误删升级前快照。
路径函数均按调用时环境变量取值，保证测试可在 import 后覆盖 DM_STATE_DIR。
"""
import os
import time
from pathlib import Path

EXPORTS_DIRNAME = "exports"
DEFAULT_KEEP_DAYS = 30        # 一键清理的默认阈值：超过 30 天的备份视为过期


def exports_dir() -> Path:
    d = Path(os.environ.get("DM_STATE_DIR", "/var/lib/dicemanager")) / EXPORTS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _info(f: Path) -> dict:
    st = f.stat()
    return {"name": f.name, "size_mb": round(st.st_size / 1048576, 1),
            "bytes": st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "age_days": max(0, int((time.time() - st.st_mtime) // 86400))}


def list_exports() -> list[dict]:
    """列出备份产物（按文件名排序）；只收一级文件，子目录不纳入。"""
    out = []
    for f in sorted(exports_dir().iterdir()):
        if f.is_file():
            try:
                out.append(_info(f))
            except OSError:                      # 正在被写入/已消失：跳过，不阻断列表
                continue
    return out


def resolve_export(name: str) -> Path:
    """名称 → 目录内文件。越界（含路径分隔符 / `..`）一律 ValueError（防目录穿越）。"""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"非法备份文件名: {name}")
    p = (exports_dir() / name).resolve()
    if p.parent != exports_dir().resolve():
        raise ValueError(f"备份文件不在产物目录内: {name}")
    if not p.is_file():
        raise ValueError(f"备份文件不存在: {name}")
    return p


def remove_export(name: str) -> bool:
    """删除单个备份；不存在返回 False（调用方映射 404）。"""
    try:
        p = resolve_export(name)
    except ValueError:
        return False
    p.unlink()
    return True


def prune_exports(days: int = DEFAULT_KEEP_DAYS) -> tuple[list[str], int]:
    """清理超过 days 天未修改的备份，返回 (被删文件名, 释放字节数)。"""
    if days < 1:
        raise ValueError("保留天数需 ≥ 1")
    cutoff = time.time() - days * 86400
    removed: list[str] = []
    freed = 0
    for f in sorted(exports_dir().iterdir()):
        try:
            if not f.is_file() or f.stat().st_mtime >= cutoff:
                continue
            size = f.stat().st_size
            f.unlink()
        except OSError:
            continue                             # 权限/占用：跳过，不阻断其余清理
        removed.append(f.name)
        freed += size
    return removed, freed
