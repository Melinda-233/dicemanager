"""清单加载与校验（坏清单直接报错）"""
import json
import re
from pathlib import Path

from adapters import (
    dicenext,
    lagrange,
    lagrange_milky,
    llbot,
    napcat,
    nonebot2,
    olivadice,
    sealdice,
    shiki,
    snowluma,
    yogurt,
)
from core.edition import is_desktop, is_server
from core.pathutil import default_install_root

# 必填字段。注意 multi_account 是「前端提示性」字段（仅 Step1 文案用），后端无任何分支；
# recommended_protocols / webui_port_bump_limit 等是纯文档字段，既不在此处要求、也不进
# /api/manifests 白名单（见 rest.list_manifests 注释），前端从不消费。
REQUIRED_KEYS = ("name", "arch", "multi_account", "exe", "install_root",
                 "required_files", "download_strategy")
ALLOWED_ARCH = ("standalone", "allinone")
# manual：上游不发行可直接运行的程序包（如 Dice! 只发平台 dll 模块），只能离线上传
# pip_project：上游是 PyPI 包而非可执行程序包（nonebot2），需 pip install --target
#   到<实例>/libs 再由入口脚本 sys.path.insert 引入
ALLOWED_STRATEGY = ("direct", "resolve_latest_via_api", "olivos_bundle_or_opk",
                    "manual", "pip_project")

classes = {"sealdice": sealdice.SealDiceAdapter, "llbot": llbot.LLBotAdapter,
           "napcat": napcat.NapCatAdapter, "shiki": shiki.ShikiAdapter,
           "olivadice": olivadice.OlivaDiceAdapter,
           "snowluma": snowluma.SnowLumaAdapter,
           "dicenext": dicenext.DiceNextAdapter,
           "lagrange": lagrange.LagrangeAdapter,
           "lagrange_milky": lagrange_milky.LagrangeMilkyAdapter,
           "yogurt": yogurt.YogurtAdapter,
           "nonebot2": nonebot2.NoneBot2Adapter}

def load_registry(manifest_dir) -> dict[str, tuple[dict, type]]:
    out: dict[str, tuple[dict, type]] = {}
    # 骰子程序安装根（desktop / server 分化 C5）：
    #   desktop → 项目根 package/（登录端与应用端均装在 package/<dice>/）。
    #     历史问题：manifest 里的 %LOCALAPPDATA% 占位符此前从未被展开（latent bug），
    #     字面路径会变成 cwd 下带 % 的怪目录。该占位符是 Windows 版的历史数据位置，
    #     Linux 版清单无此问题，故覆盖只在 desktop 生效。
    #   server → None 表示不覆盖，沿用 manifest 自带的部署位。
    # str 化后再传给 manifest：原 Path 对象会让 mypy 在赋值回同一变量时报类型冲突
    _root = default_install_root()
    install_root: str | None = None if _root is None else str(_root)
    for f in sorted(Path(manifest_dir).glob("*.json")):
        # 分化：Linux 与 Windows 清单同处一个目录（<dice>.json / <dice>_win.json），
        # 两侧 name 相同（都叫 sealdice 等），不按 edition 过滤就会互相覆盖——
        # 结果是 Windows 版拿到 Linux 的 exe 名与下载地址，部署必失败。
        if is_desktop() and not f.name.endswith("_win.json"):
            continue
        if is_server() and f.name.endswith("_win.json"):
            continue
        # 清单允许行首 // 注释（JSONC），逐行剥离后再解析
        text = re.sub(r"^\s*//.*$", "", f.read_text("utf-8"), flags=re.M)
        m = json.loads(text)
        if missing := [k for k in REQUIRED_KEYS if k not in m]:
            raise ValueError(f"manifest {f.name} 缺少字段: {missing}")
        if m["arch"] not in ALLOWED_ARCH:
            raise ValueError(f"manifest {f.name}: arch 非法")
        if m["download_strategy"] not in ALLOWED_STRATEGY:
            raise ValueError(f"manifest {f.name}: download_strategy 非法")
        if install_root is not None:
            m["install_root"] = install_root
        out[m["name"]] = (m, classes[m["name"]])
    return out
