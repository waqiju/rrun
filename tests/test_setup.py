"""setup 纯函数测试：依赖解析、装机脚本生成。"""

import rrun.setup as st
from rrun.setup import _build_bash_setup, _build_ps_setup, _req_import_name, load_requirements


class TestReqImportName:
    def test_plain(self):
        assert _req_import_name("requests") == "requests"

    def test_version_specs(self):
        assert _req_import_name("requests>=2.31") == "requests"
        assert _req_import_name("urllib3<2") == "urllib3"
        assert _req_import_name("pip==24.0") == "pip"

    def test_extras(self):
        assert _req_import_name("requests[socks]>=2") == "requests"


class TestLoadRequirements:
    def test_builtin_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(st, "REQUIREMENTS_OVERRIDE", tmp_path / "nope.txt")
        reqs = load_requirements()
        assert reqs and all(not r.startswith("#") for r in reqs)

    def test_user_override_wins(self, tmp_path, monkeypatch):
        p = tmp_path / "reqs.txt"
        p.write_text("# comment\nfoo>=1\n\nbar\n", encoding="utf-8")
        monkeypatch.setattr(st, "REQUIREMENTS_OVERRIDE", p)
        assert load_requirements() == ["foo>=1", "bar"]


class TestPbsAssets:
    def test_linux_assets_present(self):
        assert ("Linux", "x86_64") in st.PBS_ASSETS
        assert ("Linux", "aarch64") in st.PBS_ASSETS

    def test_pbs_platform_mapping(self):
        assert st._pbs_platform("Linux x86_64") == ("Linux", "x86_64")
        assert st._pbs_platform("Linux aarch64") == ("Linux", "aarch64")
        assert st._pbs_platform("Darwin arm64") == ("Mac", "arm64")
        assert st._pbs_platform("Darwin x86_64") == ("Mac", "x86_64")

    def test_pbs_platform_edge(self):
        assert st._pbs_platform("") == ("", "")
        assert st._pbs_platform("FreeBSD") == ("FreeBSD", "")

    def test_all_mapped_posix_platforms_have_assets(self):
        for uname in ("Linux x86_64", "Linux aarch64", "Darwin arm64", "Darwin x86_64"):
            assert st._pbs_platform(uname) in st.PBS_ASSETS


class TestSetupScripts:
    def test_bash_setup(self):
        s = _build_bash_setup("/usr/bin/python3.12", ["requests>=2", "foo"], force=False)
        assert '"/usr/bin/python3.12" -m venv "$venv"' in s
        assert "pip install" in s and "requests>=2 foo" in s
        assert "import requests;import foo" in s
        assert "[ \"0\" = \"1\" ]" in s  # force off

    def test_bash_setup_force(self):
        assert "[ \"1\" = \"1\" ]" in _build_bash_setup("p", ["requests"], force=True)

    def test_bash_setup_heals_half_broken_venv(self):
        # 上次 venv 创建中途失败（ensurepip 缺失等）留下的半拉子 venv：检测无 pip 并重建
        s = _build_bash_setup("/usr/bin/python3.12", ["requests"], force=False)
        assert '"$venv/bin/python" -m pip --version' in s
        assert 'rm -rf "$venv"' in s

    def test_ps_setup_ascii_and_content(self):
        s = _build_ps_setup("C:\\py\\python.exe", ["requests"], force=False)
        assert "$true" not in s and "$false" in s
        assert "pip.ini" in s and "requests" in s and "import requests" in s

    def test_ps_setup_force(self):
        assert "if($true" in _build_ps_setup("C:\\py\\python.exe", ["requests"], force=True)

    def test_ps_setup_heals_half_broken_venv(self):
        assert "-m pip --version" in _build_ps_setup("C:\\py\\python.exe", ["requests"], force=False)
