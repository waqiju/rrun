"""CLI 辅助函数测试：--env 解析、inline 落盘、--version。"""

import sys

import pytest

import rrun
import rrun.__main__ as cli


class TestParseEnv:
    def test_pairs(self):
        assert cli._parse_env(["A=1", "B=x=y"]) == {"A": "1", "B": "x=y"}

    def test_none(self):
        assert cli._parse_env(None) == {}

    def test_bad_pair(self):
        with pytest.raises(SystemExit):
            cli._parse_env(["NO_EQUALS"])


class TestDropInline:
    def test_drop_by_lang(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "INLINE_DROP_DIR", tmp_path / "drops")
        p = cli._drop_inline("mac1", "python", "print('你好')\n")
        assert p.suffix == ".py" and "mac1" in p.name
        assert p.read_text(encoding="utf-8") == "print('你好')\n"

    def test_unknown_lang_txt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "INLINE_DROP_DIR", tmp_path / "drops")
        assert cli._drop_inline("h", "weird", "x").suffix == ".txt"


class TestVersion:
    def test_version_flag(self, monkeypatch, capsys):
        # argparse version action 打印到 stdout 并以 0 退出
        monkeypatch.setattr(sys, "argv", ["rrun", "--version"])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 0
        assert capsys.readouterr().out.strip() == f"rrun {rrun.__version__}"
