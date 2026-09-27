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
    olivadice,
    sealdice,
    shiki,
    snowluma,
    yogurt,
)

# 必填字段。注意 multi_account 是「前端提示性」字段（仅 Step1 文案用），后端无任何分支；
# recommended_protocols / webui_port_bump_limit 等是纯文档字段，既不在此处要求、也不进
# /api/manifests 白名单（见 rest.list_manifests 注释），前端从不消费。
REQUIRED_KEYS = ("name", "arch", "multi_account", "exe", "install_root",
                 "required_files", "download_strategy")
ALLOWED_ARCH = ("standalone", "allinone")
# manual：上游不发行可直接运行的程序包（如 Dice! 只发平台 dll 模块），只能离线上传
ALLOWED_STRATEGY = ("direct", "resolve_latest_via_api", "olivos_bundle_or_opk",
                    "manual")

classes = {"sealdice": sealdice.SealDiceAdapter, "llbot": llbot.LLBotAdapter,
           "napcat": napcat.NapCatAdapter, "shiki": shiki.ShikiAdapter,
           "olivadice": olivadice.OlivaDiceAdapter,
           "snowluma": snowluma.SnowLumaAdapter,
           "dicenext": dicenext.DiceNextAdapter,
           "lagrange": lagrange.LagrangeAdapter,
           "lagrange_milky": lagrange_milky.LagrangeMilkyAdapter,
           "yogurt": yogurt.YogurtAdapter}

def load_registry(manifest_dir) -> dict[str, tuple[dict, type]]:
    out: dict[str, tuple[dict, type]] = {}
    for f in sorted(Path(manifest_dir).glob("*.json")):
        # 清单允许行首 // 注释（JSONC），逐行剥离后再解析
        text = re.sub(r"^\s*//.*$", "", f.read_text("utf-8"), flags=re.M)
        m = json.loads(text)
        if missing := [k for k in REQUIRED_KEYS if k not in m]:
            raise ValueError(f"manifest {f.name} 缺少字段: {missing}")
        if m["arch"] not in ALLOWED_ARCH:
            raise ValueError(f"manifest {f.name}: arch 非法")
        if m["download_strategy"] not in ALLOWED_STRATEGY:
            raise ValueError(f"manifest {f.name}: download_strategy 非法")
        out[m["name"]] = (m, classes[m["name"]])
    return out
