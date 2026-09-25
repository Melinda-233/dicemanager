"""实例备份导入：上传存档压缩包 → 解压覆盖到实例目录 → 恢复运行

格式识别与完整性校验复用 packages（按魔数、不看扩展名）。解压安全约束：
- zip-slip 防护：绝对路径 / ``..`` / 逃逸实例目录的条目直接拒绝，且**整体
  校验先于任何落盘**（不会解压一半才报错）
- 覆盖语义：只覆盖包内出现的文件，不删除包外文件（非备份内容不误删）

顶层目录歧义的消解——备份压缩包有两种常见打包方式：「压缩整个实例目录」
（所有条目共享一个顶层目录）与「压缩目录内容」（无公共顶层）。无法从包
本身百分百区分（顶层是 data 的纯数据包 vs 顶层是实例目录的全量包），因此
按落位效果择优：
1. 无公共顶层目录 → 只有一种落法，原样解压
2. 有公共顶层 → 生成「剥离 / 原样」两个候选方案，按**条目自身与祖先目录
   在实例中已存在的数量**打分，分高者胜（恢复的本质是覆盖旧数据，命中即
   意图；只比完整路径会在「目录已存在但文件名是新的」场景误判，见
   _plan_score 注释）
3. 平手（实例目录是空的/无交集）→ 取剥离方案（整目录备份剥离后才是可用实例）
"""
import shutil
import tarfile
import zipfile
from pathlib import Path

from core import packages as pkgstore


def _norm(name: str) -> str:
    return name.replace("\\", "/").strip("/")


def _safe_dest(dest: Path, rel: str) -> Path:
    """归一化条目相对路径并校验不逃逸 dest（zip-slip / 绝对路径 / ..）。"""
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise ValueError(f"压缩包内含非法路径条目: {rel}")
    p = (dest / rel).resolve()
    if not p.is_relative_to(dest.resolve()):
        raise ValueError(f"压缩包内含非法路径条目: {rel}")
    return p


def _candidate_plans(names: list[str]) -> list[dict[str, str | None]]:
    """生成候选落位方案：{原始条目名: 目标相对路径 or None(跳过)}。

    无公共顶层 → [原样]；有公共顶层且非单条目包 → [剥离, 原样]（剥离优先）。
    """
    normed = {raw: _norm(raw) for raw in names}
    valid = {raw: n for raw, n in normed.items() if n}
    if not valid:
        return [{raw: None for raw in names}]
    tops = {n.split("/", 1)[0] for n in valid.values()}
    plain = {raw: (n or None) for raw, n in normed.items()}
    if len(tops) != 1 or not any(n != next(iter(tops)) for n in valid.values()):
        return [plain]                       # 无公共顶层 / 单条目包（backup.db 不是顶层目录）
    top = next(iter(tops))
    stripped: dict[str, str | None] = {}
    for raw, n in normed.items():
        if not n:
            stripped[raw] = None
        elif n == top or n.startswith(top + "/"):
            stripped[raw] = n[len(top):].lstrip("/") or None
        else:
            stripped[raw] = n
    return [stripped, plain]


def _plan_score(dest: Path, plan: dict[str, str | None]) -> int:
    """落位方案打分：每个条目自身与其**祖先目录**在实例中已存在的数量之和。

    祖先计入是关键——实例里 data/ 已存在、但包内文件名是新的（典型纯数据
    备份）时，「原样」方案靠 data/ 祖先命中胜出；实例根级程序文件已存在、
    导入整目录备份时，「剥离」方案靠根级文件命中胜出。只比完整路径会在
    这两种场景双双退化为 0:0 平手而误判（实测踩过）。
    """
    score = 0
    for rel in plan.values():
        if not rel:
            continue
        p = dest / rel
        while True:
            if p.exists():
                score += 1
            p = p.parent
            if p == dest:
                break
    return score


def _choose_plan(dest: Path, plans: list[dict[str, str | None]]) -> dict[str, str | None]:
    """落位方案择优：命中分高者胜；平手取靠前（有剥离方案时剥离优先）。"""
    return max(plans, key=lambda pl: _plan_score(dest, pl))


def _restore_zip(archive: Path, dest: Path) -> int:
    count = 0
    with zipfile.ZipFile(archive) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        plan = _choose_plan(dest, _candidate_plans([i.filename for i in infos]))
        for rel in plan.values():
            if rel:
                _safe_dest(dest, rel)        # 解压前整体校验，杜绝半途落盘
        for info in infos:
            rel = plan[info.filename]
            if not rel:
                continue
            target = _safe_dest(dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def _restore_tar(archive: Path, dest: Path) -> int:
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        plan = _choose_plan(dest, _candidate_plans([m.name for m in members]))
        for rel in plan.values():
            if rel:
                _safe_dest(dest, rel)
        picked = []
        for m in members:
            rel = plan[m.name]               # 先查表再改名（表按原始名索引）
            if not rel:
                continue
            m.name = rel                     # 剥顶层后交给 data 过滤器按新名字校验
            picked.append(m)
        try:
            tf.extractall(dest, members=picked, filter="data")
        except TypeError:                    # 旧版本 Python 无 filter 参数（服务器 3.12+ 不会走到）
            tf.extractall(dest, members=picked)
        return sum(1 for m in picked if m.isfile())


def restore_into(archive: Path, dest: Path) -> dict:
    """校验并解压备份包到实例目录；返回 {"format": ext, "files": n}。

    校验失败 / 格式不符 / 含非法路径均抛 ValueError（由端点转 400）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    ext = pkgstore.detect_kind(archive)
    if ext is None:
        raise ValueError("不支持的压缩格式（仅接受 zip / tar.gz / tar.xz / tar.bz2 / tar）")
    pkgstore.validate_archive(archive, ext)
    count = _restore_zip(archive, dest) if ext == ".zip" else _restore_tar(archive, dest)
    return {"format": ext, "files": count}
