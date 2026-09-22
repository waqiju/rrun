"""config 子命令测试：init 模板落盘/权限、add 旗帜与向导模式、edit、空态引导。"""

import argparse
import getpass
import json
import os
import stat
from types import SimpleNamespace

import pytest

from rrun import config_cmd
from rrun.config_cmd import (
    cmd_config_add,
    cmd_config_chain,
    cmd_config_edit,
    cmd_config_init,
    user_inventory_path,
)
from rrun.registry import load_machines, resolve_machine


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """隔离全部来源：HOME 指向临时目录、cwd 切换、两个 env var 清空。"""
    monkeypatch.setenv("RRUN_CONFIG", "")
    monkeypatch.setenv("REMOTE_MACHINE_CONFIG", "")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))  # POSIX 下 Path.home() 读 HOME
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return tmp_path


def _ns(**kw):
    return argparse.Namespace(**kw)


def _flag_ns(**kw):
    base = dict(name=None, ip=None, hostname=None, os=None, user=None, password=None,
                identity_file=None, port=None, python=None, description=None, interactive=False)
    base.update(kw)
    return _ns(**base)


def _read_inventory():
    return json.loads(user_inventory_path().read_text(encoding="utf-8"))


class TestInit:
    def test_creates_template_with_secure_perms(self, isolated, capsys):
        assert cmd_config_init(_ns(force=False)) == 0
        path = user_inventory_path()
        assert path == isolated / "home" / ".rrun" / "machines.json"
        data = _read_inventory()
        assert any(m["name"] == "my-win-box" for m in data["machines"])
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert "config edit" in capsys.readouterr().out

    def test_refuses_overwrite_without_force(self, isolated, capsys):
        cmd_config_init(_ns(force=False))
        assert cmd_config_init(_ns(force=False)) == 1
        assert "--force" in capsys.readouterr().err

    def test_force_overwrites(self, isolated):
        cmd_config_init(_ns(force=False))
        user_inventory_path().write_text('{"machines": []}', encoding="utf-8")
        assert cmd_config_init(_ns(force=True)) == 0
        assert _read_inventory()["machines"]  # 模板非空

    def test_template_machines_visible_to_loader(self, isolated):
        cmd_config_init(_ns(force=False))
        assert "my-win-box" in [m.name for m in load_machines()]


class TestAddFlags:
    def test_append_creates_skeleton_file(self, isolated):
        assert cmd_config_add(_flag_ns(name="box", ip="1.2.3.4", user="admin", password="pw")) == 0
        (m,) = _read_inventory()["machines"]
        assert m == {"name": "box", "ip": "1.2.3.4", "os": "Windows", "user": "admin", "password": "pw"}
        if os.name != "nt":
            assert stat.S_IMODE(user_inventory_path().stat().st_mode) == 0o600

    def test_requires_name_ip_user(self, isolated):
        with pytest.raises(SystemExit, match="--user"):
            cmd_config_add(_flag_ns(name="box", ip="1.2.3.4"))
        with pytest.raises(SystemExit, match="--ip"):
            cmd_config_add(_flag_ns(name="box", user="u"))

    def test_whitespace_name_rejected(self, isolated):
        with pytest.raises(SystemExit, match="whitespace"):
            cmd_config_add(_flag_ns(name="my box", ip="1.2.3.4", user="u"))

    def test_bad_port_rejected(self, isolated):
        with pytest.raises(SystemExit, match="1-65535"):
            cmd_config_add(_flag_ns(name="x", ip="1.1.1.1", user="u", port=70000))

    def test_duplicate_name_rejected(self, isolated):
        cmd_config_add(_flag_ns(name="box", ip="1.2.3.4", user="u"))
        with pytest.raises(SystemExit, match="already exists"):
            cmd_config_add(_flag_ns(name="box", ip="9.9.9.9", user="u"))

    def test_appends_to_template_preserving_entries(self, isolated):
        cmd_config_init(_ns(force=False))
        cmd_config_add(_flag_ns(name="real", ip="1.2.3.4", user="root", os="Linux",
                                port=2222, identity_file="~/.ssh/id_ed25519", description="vm"))
        data = _read_inventory()
        names = [m["name"] for m in data["machines"]]
        assert "my-win-box" in names and "real" in names
        assert data["defaults"]  # 模板原有段落保留
        m = next(x for x in data["machines"] if x["name"] == "real")
        assert m == {"name": "real", "ip": "1.2.3.4", "os": "Linux", "user": "root",
                     "identity_file": "~/.ssh/id_ed25519", "port": 2222, "description": "vm"}
        # 注册表视角：key 认证 + 端口生效
        resolved = resolve_machine("real")
        assert resolved.port == 2222 and resolved.public_dict()["auth"] == "key"

    def test_port_22_not_written(self, isolated):
        cmd_config_add(_flag_ns(name="x", ip="1.1.1.1", user="u", port=22))
        assert "port" not in _read_inventory()["machines"][0]

    def test_broken_target_file_refused(self, isolated):
        path = user_inventory_path()
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(SystemExit, match="not valid JSON"):
            cmd_config_add(_flag_ns(name="x", ip="1.1.1.1", user="u"))
        assert path.read_text(encoding="utf-8") == "{broken"  # 未被覆盖

    def test_shadow_warning_when_cwd_defines_same_name(self, isolated, capsys):
        (isolated / "cwd" / "machines.json").write_text(
            json.dumps({"machines": [{"name": "box", "ip": "9.9.9.9", "user": "cwd"}]}), encoding="utf-8")
        assert cmd_config_add(_flag_ns(name="box", ip="1.2.3.4", user="u")) == 0
        assert "shadows" in capsys.readouterr().err


class TestWizard:
    def _feed(self, monkeypatch, inputs, password=""):
        it = iter(inputs)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": password)
        monkeypatch.setattr(config_cmd.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def test_full_flow_password_auth(self, isolated, monkeypatch):
        self._feed(monkeypatch, [
            "mybox",      # name
            "10.0.0.1",   # ip
            "",           # hostname
            "l",          # os -> Linux
            "ubuntu",     # user
                          # password 走 getpass mock
            "",           # port -> 22
            "",           # python
            "test box",   # description
            "Y",          # confirm
        ], password="sekret")
        assert cmd_config_add(_flag_ns()) == 0
        m = _read_inventory()["machines"][0]
        assert m == {"name": "mybox", "ip": "10.0.0.1", "os": "Linux", "user": "ubuntu",
                     "password": "sekret", "description": "test box"}

    def test_key_auth_prompts_identity_file(self, isolated, monkeypatch):
        self._feed(monkeypatch, [
            "vm", "1.2.3.4", "", "w", "admin",
            "~/.ssh/id_ed25519",  # 密码为空才问这一项
            "2222", "", "", "y",
        ], password="")
        assert cmd_config_add(_flag_ns()) == 0
        m = _read_inventory()["machines"][0]
        assert m["identity_file"] == "~/.ssh/id_ed25519" and m["port"] == 2222
        assert "password" not in m

    def test_flags_prefill_wizard_defaults(self, isolated, monkeypatch):
        # name/ip/hostname/os/user 由旗帜预填或回车，port 先输非法值触发重问再回车
        self._feed(monkeypatch, ["", "", "", "", "", "s3cr3t-port", "", "", "", "y"], password="pw")
        ns = _flag_ns(name="prefilled", ip="2.2.2.2", os="Mac", user="macuser")
        assert cmd_config_add(ns) == 0
        m = _read_inventory()["machines"][0]
        assert m["name"] == "prefilled" and m["os"] == "Mac" and m["user"] == "macuser"
        # port 交互输入非法值 -> 重问？"s3cr3t-port" 非数字会触发重问，下一个输入 "" -> 22
        assert "port" not in m

    def test_abort_writes_nothing(self, isolated, monkeypatch):
        self._feed(monkeypatch, ["x", "1.1.1.1", "", "w", "u", "", "", "", "n"], password="pw")
        assert cmd_config_add(_flag_ns()) == 1
        assert not user_inventory_path().exists()

    def test_non_tty_requires_flags(self, isolated, monkeypatch):
        monkeypatch.setattr(config_cmd.sys, "stdin", SimpleNamespace(isatty=lambda: False))
        with pytest.raises(SystemExit, match="TTY"):
            cmd_config_add(_flag_ns())

    def test_os_reprompts_on_garbage(self, isolated, monkeypatch):
        self._feed(monkeypatch, ["x", "1.1.1.1", "", "solaris", "m", "u", "", "", "", "", "y"], password="")
        assert cmd_config_add(_flag_ns()) == 0
        assert _read_inventory()["machines"][0]["os"] == "Mac"


class TestEdit:
    def test_missing_file_bootstraps_template(self, isolated, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(config_cmd.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "my-editor --wait")
        assert cmd_config_edit(_ns()) == 0
        path = user_inventory_path()
        assert path.exists() and calls == [["my-editor", "--wait", str(path)]]
        assert "created" in capsys.readouterr().out

    def test_invalid_json_after_edit_warns(self, isolated, monkeypatch, capsys):
        path = user_inventory_path()

        def fake_editor(cmd):
            path.write_text("{oops", encoding="utf-8")
            return 0

        monkeypatch.setattr(config_cmd.subprocess, "call", fake_editor)
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "true")
        assert cmd_config_edit(_ns()) == 1
        assert "does not parse" in capsys.readouterr().err

    def test_editor_nonzero_exit(self, isolated, monkeypatch):
        monkeypatch.setattr(config_cmd.subprocess, "call", lambda cmd: 130)
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "true")
        with pytest.raises(SystemExit, match="130"):
            cmd_config_edit(_ns())


class TestEmptyStateHints:
    def test_chain_diagnose_hint(self, isolated, capsys):
        assert cmd_config_chain(_ns()) == 0
        assert "rrun config init" in capsys.readouterr().out

    def test_machines_hint(self, isolated, capsys):
        import rrun.__main__ as cli
        assert cli.cmd_machines(_ns(json=False)) == 0
        assert "rrun config init" in capsys.readouterr().err

    def test_resolve_machine_hint(self, isolated):
        with pytest.raises(KeyError, match="rrun config init"):
            resolve_machine("nope")


class TestInventoryPathCollision:
    """v0.2.3 回归：cwd == ~/.rrun 或 $RRUN_CONFIG 指向用户清单时，来源链去重会丢弃
    user 标签——user_inventory_path() 不得受此影响。"""

    def test_cwd_inside_rrun_home(self, isolated, monkeypatch):
        cmd_config_init(_ns(force=False))
        monkeypatch.chdir(isolated / "home" / ".rrun")
        assert cmd_config_add(_flag_ns(name="x", ip="1.1.1.1", user="u")) == 0
        assert resolve_machine("x").ip == "1.1.1.1"
        assert cmd_config_init(_ns(force=False)) == 1  # 仍能正确识别已存在

    def test_env_points_at_user_file(self, isolated, monkeypatch):
        cmd_config_init(_ns(force=False))
        monkeypatch.setenv("RRUN_CONFIG", str(user_inventory_path()))
        assert cmd_config_add(_flag_ns(name="x", ip="1.1.1.1", user="u")) == 0
        assert resolve_machine("x").ip == "1.1.1.1"


class TestMachinesD:
    def test_init_creates_dir_and_readme(self, isolated):
        cmd_config_init(_ns(force=False))
        d = user_inventory_path().parent / "machines.d"
        readme = d / "README.md"
        assert readme.is_file() and "ln -s" in readme.read_text(encoding="utf-8")
        if os.name != "nt":
            assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_readme_not_treated_as_inventory(self, isolated):
        cmd_config_init(_ns(force=False))
        assert all("README" not in m.name for m in load_machines())

    def test_ensure_machines_d_idempotent(self, isolated):
        cmd_config_init(_ns(force=False))
        readme = user_inventory_path().parent / "machines.d" / "README.md"
        readme.write_text("user customized", encoding="utf-8")
        cmd_config_init(_ns(force=True))
        assert readme.read_text(encoding="utf-8") == "user customized"  # 不覆盖用户改动

    def test_chain_shows_empty_dir(self, isolated, capsys):
        cmd_config_init(_ns(force=False))
        capsys.readouterr()
        cmd_config_chain(_ns())
        assert "no *.json files" in capsys.readouterr().out

    def test_chain_lists_json_files_when_present(self, isolated, capsys):
        cmd_config_init(_ns(force=False))
        d = user_inventory_path().parent / "machines.d"
        (d / "10-x.json").write_text(
            json.dumps({"machines": [{"name": "deep", "ip": "1.1.1.1", "user": "u"}]}),
            encoding="utf-8")
        capsys.readouterr()
        cmd_config_chain(_ns())
        out = capsys.readouterr().out
        assert "10-x.json" in out and "no *.json" not in out
        assert "deep" in [m.name for m in load_machines()]


class TestTemplateHint:
    def test_hint_present_and_ignored_by_loader(self, isolated):
        cmd_config_init(_ns(force=False))
        assert "_hint" in _read_inventory()
        assert load_machines()  # _hint 不影响加载

    def test_hint_preserved_after_add(self, isolated):
        cmd_config_init(_ns(force=False))
        cmd_config_add(_flag_ns(name="z", ip="1.1.1.1", user="u"))
        assert "_hint" in _read_inventory()
