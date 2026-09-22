"""executor 纯函数测试：PS wrapper、bash 命令、ASCII 校验、ssh 参数组装。"""

import base64

import pytest

from rrun.executor import (
    _check_ascii,
    _posix_prefix,
    _ps_quote,
    _ssh_args,
    build_bash_command,
    build_ps_wrapper,
)
from rrun.registry import Machine


def _win(**kw):
    kw.setdefault("os", "Windows")
    return Machine(name="w", ip="1.1.1.1", user="u", **kw)


class TestCheckAscii:
    def test_ascii_ok(self):
        _check_ascii(["abc", "x=1"], "args")

    @pytest.mark.parametrize("bad", ["中文", "café", "a​b"])
    def test_non_ascii_rejected(self, bad):
        with pytest.raises(ValueError, match="ASCII"):
            _check_ascii([bad], "args")


class TestPsWrapper:
    def test_pure_ascii_single_line(self):
        payload = build_ps_wrapper('Write-Output "你好，世界"')
        assert payload.count(b"\n") == 1
        payload.decode("ascii")  # 必须纯 ASCII

    def test_script_content_roundtrips_via_base64(self):
        code = 'Write-Output "你好 $HOME `n"'
        line = build_ps_wrapper(code).decode("ascii")
        b64 = line.split("FromBase64String('")[1].split("'")[0]
        assert base64.b64decode(b64).decode("utf-8") == code

    def test_args_workdir_env(self):
        line = build_ps_wrapper("x", args=["a", "b'c"], workdir="C:\\work",
                                env={"K": "v"}).decode("ascii")
        assert "Set-Location 'C:\\work'" in line
        assert "$env:K='v'" in line
        assert "'b''c'" in line  # PS 单引号转义
        assert "$__args=@('a','b''c')" in line

    def test_non_ascii_args_rejected(self):
        with pytest.raises(ValueError, match="ASCII"):
            build_ps_wrapper("x", args=["中文"])

    def test_exit_code_passthrough_tail(self):
        line = build_ps_wrapper("x").decode("ascii")
        assert "exit $LASTEXITCODE" in line
        assert "ScriptBlock" in line


class TestBashCommand:
    def test_plain(self):
        assert build_bash_command() == "bash -s"

    def test_args_workdir_env(self):
        cmd = build_bash_command(args=["a b", "c"], workdir="/tmp/x y", env={"K": "v v"})
        assert cmd.startswith("cd '/tmp/x y' && env K='v v' bash -s -- ")
        assert "'a b'" in cmd and cmd.endswith(" c")

    def test_non_ascii_args_rejected(self):
        with pytest.raises(ValueError, match="ASCII"):
            build_bash_command(args=["中文"])


class TestPosixPrefix:
    def test_empty(self):
        assert _posix_prefix() == ""

    def test_quoting(self):
        assert _posix_prefix(workdir="a b") == "cd 'a b' && "
        assert _posix_prefix(env={"K": "v v"}) == "env K='v v' "


class TestPsQuote:
    def test_escape(self):
        assert _ps_quote("a'b") == "'a''b'"


class TestSshArgs:
    def test_password_uses_sshpass(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/sshpass" if name == "sshpass" else None)
        args = _ssh_args(_win(password="pw"), mux=False)
        assert args[:3] == ["/usr/bin/sshpass", "-p", "pw"]
        assert "NumberOfPasswordPrompts=1" in args
        assert "BatchMode=yes" not in args

    def test_password_without_sshpass_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(RuntimeError, match="sshpass not found"):
            _ssh_args(_win(password="pw"), mux=False)

    def test_key_auth_needs_no_sshpass(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)  # 无 sshpass 也能走 key
        args = _ssh_args(_win(password="", identity_file="~/.ssh/id_ed25519"), mux=False)
        assert args[0] == "ssh"
        assert "BatchMode=yes" in args
        i = args.index("-i")
        assert args[i + 1].endswith(".ssh/id_ed25519")

    def test_custom_port(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/sshpass")
        args = _ssh_args(_win(password="pw", port=2222), mux=False)
        i = args.index("-p", 2)  # 跳过 sshpass -p
        assert args[i + 1] == "2222"

    def test_default_port_no_flag(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/sshpass")
        args = _ssh_args(_win(password="pw"), mux=False)
        assert "-p" not in args[3:]  # sshpass 的 -p 之后不再有 -p

    def test_mux_adds_control_opts(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/sshpass")
        import rrun.executor as ex
        monkeypatch.setattr(ex, "CONTROL_PATH_DIR", tmp_path / ".ssh")
        args = _ssh_args(_win(password="pw"), mux=True)
        assert "ControlMaster=auto" in " ".join(args)
        assert (tmp_path / ".ssh").is_dir()
