"""transfer 纯函数测试：远端命令组装（ASCII/注入安全/协议不变量）、路径语义、回执解析。"""

import base64
import shlex

from rrun.registry import Machine
from rrun.transfer import (
    _POSIX_PULL_SCRIPT,
    _POSIX_PUSH_SCRIPT,
    _PS_PULL_BODY,
    _parse_dest,
    _parse_stderr,
    _remote_basename,
    _resolve_local_dest,
    build_posix_pull_command,
    build_posix_push_command,
    build_win_pull_command,
    build_win_push_append,
    build_win_push_finalize,
    build_win_push_init,
    build_win_push_single,
)


def _win(**kw):
    kw.setdefault("os", "Windows")
    return Machine(name="w", ip="1.1.1.1", user="u", **kw)


def _posix(**kw):
    kw.setdefault("os", "Linux")
    return Machine(name="m", ip="2.2.2.2", user="u", **kw)


class TestPosixPushCommand:
    def test_static_script_plus_argv(self):
        cmd = build_posix_push_command(_posix(), "/tmp/目标 file.txt", "目标 file.txt", 123, "ab" * 32)
        tokens = shlex.split(cmd)
        assert tokens[0] == "bash" and tokens[1] == "-c"
        assert tokens[2] == _POSIX_PUSH_SCRIPT  # 脚本静态，路径只在 argv
        assert tokens[3] == "--"
        assert tokens[4] == "/tmp/目标 file.txt"
        assert tokens[5] == "目标 file.txt"
        assert tokens[6] == "123"
        assert tokens[7] == "ab" * 32

    def test_script_is_pure_ascii(self):
        # argv 里才有 CJK；脚本本体必须纯 ASCII（经远端登录 shell 解析）
        _POSIX_PUSH_SCRIPT.encode("ascii")

    def test_injection_safe_quoting(self):
        evil = "x'; rm -rf / #"
        tokens = shlex.split(build_posix_push_command(_posix(), evil, "b", 1, ""))
        assert tokens[4] == evil  # shlex.quote 原样往返


class TestPosixPullCommand:
    def test_static_script_plus_argv(self):
        tokens = shlex.split(build_posix_pull_command(_posix(), "~/日志/a.txt"))
        assert tokens[2] == _POSIX_PULL_SCRIPT
        assert tokens[4] == "~/日志/a.txt"


class TestWindowsPushBuilders:
    """Windows push = -Command - 单行命令序列（OpenSSH-Windows stdin 泵不可靠的规避设计）。"""

    def _assert_ascii_single_line(self, line: str):
        line.encode("ascii")
        assert "\n" not in line

    def test_single_shot(self):
        line = build_win_push_single(r"C:\数据\file's.txt", "file's.txt", 5, "cd" * 32, "aabbccdd", b"hello")
        self._assert_ascii_single_line(line)
        assert "$ProgressPreference='SilentlyContinue'" in line
        assert "ReadLine" not in line  # 禁用项：重定向 stdin + ReadLine 间歇挂起
        # 路径经 base64(utf-8) 内嵌，可解码回原文
        dest_b64 = base64.b64encode(r"C:\数据\file's.txt".encode()).decode()
        assert dest_b64 in line
        assert base64.b64encode(b"hello").decode() in line  # 数据同通道内嵌
        assert "$wantSize=5" in line and f"$wantSha='{'cd' * 32}'" in line
        assert "rrun-tmp-aabbccdd" in line
        assert "RRUN $size $sha" in line  # 回执

    def test_chunked_protocol(self):
        init = build_win_push_init("~/x/y.txt", "y.txt", "aabbccdd")
        self._assert_ascii_single_line(init)
        assert "RRUN-DEST-B64" in init

        append = build_win_push_append(r"C:\tmp\f.rrun-tmp-aabbccdd", b"x" * 10)
        self._assert_ascii_single_line(append)
        assert "Append" in append

        fin = build_win_push_finalize(r"C:\数据\y.txt", r"C:\数据\y.txt.rrun-tmp-aabbccdd", 7, "ef" * 32)
        self._assert_ascii_single_line(fin)
        assert "$wantSize=7" in fin
        assert "Move-Item" in fin and "RRUN $size $sha" in fin

    def test_all_builders_ascii_with_cjk_paths(self):
        cjk = "~/子 目录/文件.txt"
        for line in (
            build_win_push_single(cjk, "文件.txt", 0, "", "00", b""),
            build_win_push_init(cjk, "文件.txt", "00"),
            build_win_push_append(cjk, b""),
            build_win_push_finalize(cjk, cjk + ".rrun-tmp-00", 0, ""),
        ):
            self._assert_ascii_single_line(line)


class TestWindowsPullCommand:
    def test_encoded_command(self):
        cmd = build_win_pull_command("~/日志/a.txt")
        assert cmd.startswith("powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ")
        cmd.encode("ascii")
        script = base64.b64decode(cmd.rsplit(" ", 1)[1]).decode("utf-16-le")
        assert _PS_PULL_BODY.strip() in script
        src_b64_line = base64.b64encode("~/日志/a.txt".encode()).decode()
        assert src_b64_line in script  # 路径 base64 内嵌

    def test_pull_body_protocol_invariants(self):
        assert "$ProgressPreference='SilentlyContinue'" in _PS_PULL_BODY  # CLIXML 噪声抑制
        assert "OpenStandardOutput" in _PS_PULL_BODY  # 裸二进制出流


class TestRemoteBasename:
    def test_posix(self):
        assert _remote_basename("/a/b/c.txt", False) == "c.txt"
        assert _remote_basename("~/c.txt", False) == "c.txt"
        assert _remote_basename("/a/b/", False) == "b"

    def test_windows(self):
        assert _remote_basename(r"C:\a\b\c.txt", True) == "c.txt"
        assert _remote_basename("C:/a/b/c.txt", True) == "c.txt"
        assert _remote_basename("~/c.txt", True) == "c.txt"


class TestResolveLocalDest:
    def test_plain_file(self, tmp_path):
        dest = _resolve_local_dest(str(tmp_path / "out.txt"), "remote.txt")
        assert dest == tmp_path / "out.txt"

    def test_existing_dir_keeps_basename(self, tmp_path):
        dest = _resolve_local_dest(str(tmp_path), "remote.txt")
        assert dest == tmp_path / "remote.txt"

    def test_trailing_slash_keeps_basename(self, tmp_path):
        dest = _resolve_local_dest(str(tmp_path / "newdir") + "/", "remote.txt")
        assert dest == tmp_path / "newdir" / "remote.txt"


class TestParseStderr:
    def test_receipt(self):
        err = b"some noise\nRRUN 123 " + b"ab" * 32 + b"\r\n"
        assert _parse_stderr(err) == (123, "ab" * 32, "")

    def test_error(self):
        assert _parse_stderr(b"RRUN-ERROR remote file not found: /x\n") == (-1, "", "remote file not found: /x")

    def test_receipt_and_error(self):
        err = b"RRUN-ERROR boom\nRRUN 7 " + b"cd" * 32 + b"\n"
        assert _parse_stderr(err) == (7, "cd" * 32, "boom")

    def test_garbage(self):
        assert _parse_stderr(b"\xff\xfe garbage\n") == (-1, "", "")

    def test_clixml_noise_ignored(self):
        # OpenSSH-Windows stderr 混入 CLIXML 进度记录时仍能解析回执
        err = b"#< CLIXML\r\n<Objs Version=\"1.1.0.1\"><Obj S=\"progress\"/></Objs>\r\nRRUN 5 " + b"ef" * 32 + b"\r\n"
        assert _parse_stderr(err) == (5, "ef" * 32, "")


class TestParseDest:
    def test_roundtrip(self):
        b64 = base64.b64encode(r"C:\数据\y.txt".encode()).decode()
        assert _parse_dest(f"RRUN-DEST-B64 {b64}\r\n".encode()) == r"C:\数据\y.txt"

    def test_absent(self):
        assert _parse_dest(b"nothing here") == ""
