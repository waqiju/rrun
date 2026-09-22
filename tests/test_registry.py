"""registry 纯函数测试：来源链、merge 优先级、defaults、端口/key 字段。"""

import json
import os

import pytest

from rrun import registry
from rrun.registry import Machine, _load_file, load_machines, resolve_machine


def _write(path, machines, defaults=None):
    data = {"machines": machines}
    if defaults:
        data["defaults"] = defaults
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """隔离全部来源：HOME 指向临时目录、cwd 切换、两个 env var 清空。"""
    monkeypatch.setenv("RRUN_CONFIG", "")
    monkeypatch.setenv("REMOTE_MACHINE_CONFIG", "")
    home = tmp_path / "home"
    (home / ".rrun").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))  # POSIX 下 Path.home() 读 HOME
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return tmp_path


class TestMachine:
    def test_defaults(self):
        m = Machine(name="a", ip="1.1.1.1", user="u")
        assert m.port == 22 and m.identity_file == "" and m.password == ""
        assert m.os == "Windows" and m.is_windows and m.default_lang == "powershell"

    def test_public_dict_redacts_password(self):
        m = Machine(name="a", ip="1.1.1.1", user="u", password="secret")
        d = m.public_dict()
        assert "secret" not in json.dumps(d)
        assert d["auth"] == "password"
        m2 = Machine(name="b", ip="2.2.2.2", user="u")
        assert m2.public_dict()["auth"] == "key"

    def test_password_not_in_repr(self):
        assert "secret" not in repr(Machine(name="a", ip="1.1.1.1", user="u", password="secret"))


class TestLoadFile:
    def test_defaults_applied_per_os(self, tmp_path):
        p = _write(
            tmp_path / "m.json",
            [{"name": "w1", "ip": "1.1.1.1", "os": "Windows"},
             {"name": "m1", "ip": "2.2.2.2", "os": "Mac", "user": "macuser"}],
            defaults={"windows": {"user": "winuser", "password": "wpw"},
                      "mac": {"password": "mpw"}},
        )
        machines = {m.name: m for m in _load_file(p)}
        assert machines["w1"].user == "winuser" and machines["w1"].password == "wpw"
        assert machines["m1"].user == "macuser" and machines["m1"].password == "mpw"

    def test_port_and_identity_file(self, tmp_path):
        p = _write(tmp_path / "m.json",
                   [{"name": "a", "ip": "1.1.1.1", "user": "u", "port": 2222,
                     "identity_file": "~/.ssh/id_ed25519"}])
        (m,) = _load_file(p)
        assert m.port == 2222 and m.identity_file == "~/.ssh/id_ed25519"

    def test_port_string_cast_to_int(self, tmp_path):
        p = _write(tmp_path / "m.json", [{"name": "a", "ip": "1.1.1.1", "user": "u", "port": "2222"}])
        assert _load_file(p)[0].port == 2222

    def test_unknown_keys_ignored(self, tmp_path):
        p = _write(tmp_path / "m.json", [{"name": "a", "ip": "1.1.1.1", "user": "u", "zzz": 1}])
        assert _load_file(p)[0].name == "a"


class TestSourceChain:
    def test_cwd_beats_user(self, isolated):
        _write(isolated / "cwd" / "machines.json",
               [{"name": "a", "ip": "1.1.1.1", "user": "cwd_user"}])
        _write(isolated / "home" / ".rrun" / "machines.json",
               [{"name": "a", "ip": "1.1.1.1", "user": "home_user"}])
        (m,) = load_machines()
        assert m.user == "cwd_user"

    def test_env_beats_cwd_and_multi_path(self, isolated, monkeypatch):
        f1 = _write(isolated / "env1.json", [{"name": "a", "ip": "1.1.1.1", "user": "env_user"}])
        f2 = _write(isolated / "env2.json", [{"name": "b", "ip": "2.2.2.2", "user": "u2"}])
        _write(isolated / "cwd" / "machines.json", [{"name": "a", "ip": "1.1.1.1", "user": "cwd"}])
        monkeypatch.setenv("RRUN_CONFIG", os.pathsep.join([str(f1), str(f2)]))
        machines = {m.name: m for m in load_machines()}
        assert machines["a"].user == "env_user" and "b" in machines

    def test_machines_d_sorted_and_below_user(self, isolated):
        rrun_home = isolated / "home" / ".rrun"
        (rrun_home / "machines.d").mkdir(parents=True)
        _write(rrun_home / "machines.json", [{"name": "a", "ip": "1.1.1.1", "user": "user_main"}])
        _write(rrun_home / "machines.d" / "10-x.json",
               [{"name": "a", "ip": "1.1.1.1", "user": "from_d"}, {"name": "b", "ip": "2.2.2.2", "user": "u"}])
        machines = {m.name: m for m in load_machines()}
        assert machines["a"].user == "user_main" and machines["b"].user == "u"

    def test_broken_source_skipped(self, isolated):
        (isolated / "cwd" / "machines.json").write_text("{not json", encoding="utf-8")
        _write(isolated / "home" / ".rrun" / "machines.json",
               [{"name": "a", "ip": "1.1.1.1", "user": "u"}])
        assert [m.name for m in load_machines()] == ["a"]
        errors = [i for i in registry.scan_sources() if i.error]
        assert len(errors) == 1

    def test_explicit_path_single_file_mode(self, isolated, tmp_path):
        _write(isolated / "cwd" / "machines.json", [{"name": "cwd", "ip": "1.1.1.1", "user": "u"}])
        p = _write(tmp_path / "pinned.json", [{"name": "pinned", "ip": "2.2.2.2", "user": "u"}])
        assert [m.name for m in load_machines(p)] == ["pinned"]


class TestResolve:
    def test_by_name_ip_hostname(self, tmp_path):
        p = _write(tmp_path / "m.json",
                   [{"name": "a", "ip": "1.1.1.1", "user": "u", "hostname": "host-a"}])
        for key in ("a", "1.1.1.1", "host-a"):
            assert resolve_machine(key, p).name == "a"

    def test_not_found_lists_available(self, tmp_path):
        p = _write(tmp_path / "m.json", [{"name": "a", "ip": "1.1.1.1", "user": "u"}])
        with pytest.raises(KeyError, match="a\\(1.1.1.1\\)"):
            resolve_machine("nope", p)
