"""sealdice 互联端点写入回归（2026-09-25 备份恢复事故）。

备份来自旧机器时，serve.yaml 的 imSession.endPoints 会原样带回旧机的
onebot 端点（connectUrl 指向 127.0.0.1:12345 之类）。write_conn_config 必须：
1) 以 baseInfo.id 认领本实例端点（改地址也原地更新，不重复追加）；
2) 把其余 onebot 端点停用，否则旧端点持续拨号报 connection refused。"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.sealdice import SealDiceAdapter  # noqa: E402


def _make(tmp_path):
    a = SealDiceAdapter({"name": "sealdice", "exe": "sealdice-core"})
    inst = type("I", (), {"id": "sealdice-x", "dir": str(tmp_path),
                          "dice": "sealdice", "arch": "standalone",
                          "port": 3211})()
    d = tmp_path / "data" / "default"
    d.mkdir(parents=True)
    (d / "serve.yaml").write_text("commandPrefix: ['.']\n", encoding="utf-8")
    return a, inst, d / "serve.yaml"


def _load(p):
    return yaml.safe_load(p.read_text("utf-8"))


def test_write_appends_and_sets_own_id(tmp_path):
    a, inst, path = _make(tmp_path)
    a.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "tok")
    eps = _load(path)["imSession"]["endPoints"]
    assert len(eps) == 1
    assert eps[0]["baseInfo"]["id"] == "sealdice-x"
    assert eps[0]["adapter"]["connectUrl"] == "ws://127.0.0.1:3001"
    assert eps[0]["adapter"]["accessToken"] == "tok"


def test_write_updates_own_endpoint_in_place(tmp_path):
    """端点地址变化（如 login_ref 换绑）必须原地更新，不能重复追加。"""
    a, inst, path = _make(tmp_path)
    a.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "tok")
    a.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3999", "tok2")
    eps = _load(path)["imSession"]["endPoints"]
    assert len(eps) == 1
    assert eps[0]["adapter"]["connectUrl"] == "ws://127.0.0.1:3999"
    assert eps[0]["adapter"]["accessToken"] == "tok2"


def test_write_disables_foreign_endpoints_from_backup(tmp_path):
    """备份恢复场景：旧机端点（12345）写入后必须被停用，新端点正常工作。"""
    a, inst, path = _make(tmp_path)
    old = {"baseInfo": {"id": "da651428-6237-old", "state": 2, "platform": "QQ",
                        "protocolType": "onebot", "enable": True,
                        "userId": "QQ:975809162"},
           "adapter": {"isReverse": False, "connectUrl": "ws://127.0.0.1:12345",
                       "accessToken": "54321"}}
    path.write_text(yaml.safe_dump({"imSession": {"endPoints": [old]}}),
                    encoding="utf-8")
    a.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "tok")
    eps = _load(path)["imSession"]["endPoints"]
    assert len(eps) == 2
    mine = [e for e in eps if e["baseInfo"]["id"] == "sealdice-x"]
    foreign = [e for e in eps if e["baseInfo"]["id"] != "sealdice-x"]
    assert len(mine) == 1 and mine[0]["baseInfo"]["enable"] is True
    assert mine[0]["adapter"]["connectUrl"] == "ws://127.0.0.1:3001"
    assert len(foreign) == 1 and foreign[0]["baseInfo"]["enable"] is False


def test_write_milky_forward_uses_http_base(tmp_path):
    """Milky 协议：connectUrl 是 http:// 基址（HTTP API + /event WS），不是 ws://。"""
    a, inst, path = _make(tmp_path)
    a.write_conn_config(inst, "milky", "forward", "127.0.0.1:3000", "tok")
    eps = _load(path)["imSession"]["endPoints"]
    assert len(eps) == 1
    assert eps[0]["baseInfo"]["protocolType"] == "milky"
    assert eps[0]["adapter"]["connectUrl"] == "http://127.0.0.1:3000"
    assert eps[0]["adapter"]["accessToken"] == "tok"
    assert eps[0]["adapter"]["reverseAddr"] == ""


def test_write_milky_reverse_appends_event_suffix(tmp_path):
    """反向：登录端连海豹，Milky 事件流地址带 /event 后缀（不是 OneBot 的 /ws）。"""
    a, inst, path = _make(tmp_path)
    a.write_conn_config(inst, "milky", "reverse", "127.0.0.1:3000", "tok")
    eps = _load(path)["imSession"]["endPoints"]
    assert eps[0]["baseInfo"]["protocolType"] == "milky"
    assert eps[0]["adapter"]["isReverse"] is True
    assert eps[0]["adapter"]["reverseAddr"] == "127.0.0.1:3000/event"
    assert eps[0]["adapter"]["connectUrl"] == ""


def test_write_disables_foreign_milky_not_foreign_onebot(tmp_path):
    """备份恢复场景：同协议（milky）的外来端点停用；不同协议（onebot）的外来端点保留。"""
    a, inst, path = _make(tmp_path)
    old_milky = {"baseInfo": {"id": "x-milky-old", "state": 2, "platform": "QQ",
                              "protocolType": "milky", "enable": True,
                              "userId": "QQ:111"},
                 "adapter": {"isReverse": False, "connectUrl": "http://127.0.0.1:9999",
                             "accessToken": "a"}}
    old_onebot = {"baseInfo": {"id": "x-ob-old", "state": 2, "platform": "QQ",
                               "protocolType": "onebot", "enable": True,
                               "userId": "QQ:222"},
                  "adapter": {"isReverse": False, "connectUrl": "ws://127.0.0.1:12345",
                              "accessToken": "b"}}
    path.write_text(yaml.safe_dump(
        {"imSession": {"endPoints": [old_milky, old_onebot]}}), encoding="utf-8")
    a.write_conn_config(inst, "milky", "forward", "127.0.0.1:3000", "tok")
    eps = _load(path)["imSession"]["endPoints"]
    assert len(eps) == 3
    by_id = {e["baseInfo"]["id"]: e for e in eps}
    assert by_id["sealdice-x"]["baseInfo"]["enable"] is True
    assert by_id["sealdice-x"]["adapter"]["connectUrl"] == "http://127.0.0.1:3000"
    # 同协议（milky）外来的被停用；不同协议（onebot）外来的保持启用
    assert by_id["x-milky-old"]["baseInfo"]["enable"] is False
    assert by_id["x-ob-old"]["baseInfo"]["enable"] is True


def test_write_preserves_rfc3339_timestamps(tmp_path):
    """pyyaml 把 lastSavedTime 的 RFC3339 字符串解析成 datetime 后，safe_dump
    会丢掉 'T'（2026-09-25 18:17:38...），海豹严格解析直接 panic（2026-09-25
    线上事故）。写回后必须仍是带 T 的 RFC3339 格式。"""
    a, inst, path = _make(tmp_path)
    path.write_text(
        "lastSavedTime: 2026-09-25T18:17:38.182038989+08:00\n"
        "imSession:\n"
        "    endPoints: []\n",
        encoding="utf-8")
    a.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "tok")
    raw = path.read_text("utf-8")
    # pyyaml load 阶段纳秒已截断为微秒（.182038989 → .182038），但 'T' 必须保留；
    # safe_dump 会给含冒号的值加引号，Go yaml 解析时等价
    assert "2026-09-25T18:17:38.182038+08:00" in raw
    assert "lastSavedTime: 2026-09-25 " not in raw        # 丢 T 的坏格式不得出现


def test_write_dedup_by_connect_url_for_legacy_entries(tmp_path):
    """旧版本写入的端点无本实例 id，按 connectUrl 查重命中后原地收编。"""
    a, inst, path = _make(tmp_path)
    legacy = {"baseInfo": {"id": "sealdice-x", "state": 0, "platform": "QQ",
                           "protocolType": "onebot", "enable": True},
              "adapter": {"isReverse": False,
                          "connectUrl": "ws://127.0.0.1:3001",
                          "accessToken": "old"}}
    path.write_text(yaml.safe_dump({"imSession": {"endPoints": [legacy]}}),
                    encoding="utf-8")
    a.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "new")
    eps = _load(path)["imSession"]["endPoints"]
    assert len(eps) == 1
    assert eps[0]["adapter"]["accessToken"] == "new"
