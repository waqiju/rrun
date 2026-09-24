"""文件传输：本地文件 ↔ 远端机器，走 ssh 管道（设计见 docs/push-pull-design.md）。

要点：
- push 数据走 stdin；pull 数据走 stdout。stdout 只载货，状态/回执全走 stderr。
- 载荷全程裸二进制：POSIX push 经 stdin 直推（EOF 收尾）；
  Windows push 走 **exec 同款 `-Command -` 通道的分块追加**——实机调试结论：
  OpenSSH-Windows（9.5p1 实测）的 stdin 泵在「进程启动后到达的数据 + 流控」下
  投递不可靠（ReadLine/OpenStandardInput 均挂），但 `-Command -` 通道下 1MB 级
  单行载荷稳定（exec 每日在用）。故 Windows push = 单行 ASCII PS 命令序列：
  ≤512KB 一次成形；更大则 init → append×N → finalize。
- 所有 Windows 载荷（路径、数据）一律 base64 内嵌——命令行/stdin 全程纯 ASCII，CJK 免疫。
- 原子替换：写 <dest>.rrun-tmp-<token>，sha256 校验通过才 rename；不符删临时文件，exit 3。
- 退出码：0 成功 / 1 远端失败 / 2 本地用法错误 / 3 完整性不符 / 255 传输层 / 124 本地超时。
"""

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from .executor import (
    AUDIT_LOG,
    EXIT_TIMEOUT,
    EXIT_TRANSPORT_ERROR,
    PS_REMOTE_CMD,
    _ssh_args,
    _ssh_run,
)
from .registry import Machine, resolve_machine

EXIT_INTEGRITY = 3  # 完整性校验不符（push：远端已删临时文件；pull：本地已删临时文件）

_WIN_CHUNK = 512 * 1024   # Windows 分块上传的单块载荷（base64 后 ~683KB 单行，实测通道稳）
_IO_CHUNK = 256 * 1024    # 本地读文件 / POSIX 流的块大小

# ---------------------------------------------------------------------------
# 远端脚本（POSIX：静态常量 + argv 传参，从构造上免疫注入）
# argv 顺序：dest / base（目录目标时追加的文件名）/ want_size（-1=未知）/ want_sha（空=未知）
# ---------------------------------------------------------------------------
_POSIX_PUSH_SCRIPT = """set -eu
dest=$1
base=$2
want_size=$3
want_sha=$4
case $dest in
  ~) dest=$HOME ;;
  ~/*) dest=$HOME/${dest#~/} ;;
esac
case $dest in
  */) dest=${dest%/}/$base ;;
  *) if [ -d "$dest" ]; then dest=$dest/$base; fi ;;
esac
parent=${dest%/*}
if [ -z "$parent" ]; then parent=/; elif [ "$parent" = "$dest" ]; then parent=.; fi
mkdir -p "$parent"
tmp=$dest.rrun-tmp-$$
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
size=$(wc -c < "$tmp" | tr -d ' ')
if [ "$want_size" -ge 0 ] && [ "$size" != "$want_size" ]; then
  echo "RRUN-ERROR size mismatch: expected $want_size, got $size" >&2
  exit 3
fi
if command -v sha256sum >/dev/null 2>&1; then
  sha=$(sha256sum "$tmp" | cut -d ' ' -f 1)
else
  sha=$(shasum -a 256 "$tmp" | cut -d ' ' -f 1)
fi
if [ -n "$want_sha" ] && [ "$sha" != "$want_sha" ]; then
  echo "RRUN-ERROR sha256 mismatch: expected $want_sha, got $sha" >&2
  exit 3
fi
mv -f "$tmp" "$dest"
trap - EXIT
echo "RRUN $size $sha" >&2
"""

_POSIX_PULL_SCRIPT = """set -eu
src=$1
case $src in
  ~) src=$HOME ;;
  ~/*) src=$HOME/${src#~/} ;;
esac
if [ -d "$src" ]; then
  echo "RRUN-ERROR remote path is a directory (recursive transfer not supported): $src" >&2
  exit 1
fi
if [ ! -f "$src" ]; then
  echo "RRUN-ERROR remote file not found: $src" >&2
  exit 1
fi
size=$(wc -c < "$src" | tr -d ' ')
if command -v sha256sum >/dev/null 2>&1; then
  sha=$(sha256sum "$src" | cut -d ' ' -f 1)
else
  sha=$(shasum -a 256 "$src" | cut -d ' ' -f 1)
fi
echo "RRUN $size $sha" >&2
cat "$src"
"""

# Windows pull：裸二进制直接写 stdout 流（字节安全，实机 sha256 一致）；
# 路径嵌入脚本头部后整体 -EncodedCommand（纯 ASCII，CJK 免疫）；回执先于数据写 stderr。
# $ProgressPreference 必须压制：否则 stderr 混入 CLIXML 进度噪声。
_PS_PULL_BODY = """
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$fs=$null
try{
  if($src -eq '~'){$src=$HOME}
  elseif($src.StartsWith('~/') -or $src.StartsWith('~\\')){$src=Join-Path $HOME $src.Substring(2)}
  if(Test-Path -LiteralPath $src -PathType Container){
    [Console]::Error.WriteLine("RRUN-ERROR remote path is a directory (recursive transfer not supported): $src")
    exit 1
  }
  if(-not (Test-Path -LiteralPath $src -PathType Leaf)){
    [Console]::Error.WriteLine("RRUN-ERROR remote file not found: $src")
    exit 1
  }
  $size=[long](Get-Item -LiteralPath $src).Length
  $sha=(Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash.ToLower()
  [Console]::Error.WriteLine("RRUN $size $sha")
  $fs=[IO.File]::OpenRead($src)
  $out=[Console]::OpenStandardOutput()
  $buf=New-Object byte[] 262144
  while(($n=$fs.Read($buf,0,$buf.Length)) -gt 0){$out.Write($buf,0,$n)}
  $out.Flush()
  $fs.Close();$fs=$null
  exit 0
}catch{
  if($fs){try{$fs.Close()}catch{}}
  [Console]::Error.WriteLine("RRUN-ERROR $($_.Exception.Message)")
  exit 1
}
"""

# ---------------------------------------------------------------------------
# Windows push 的单行 PS 构件（经 -Command - 下发；必须单行、纯 ASCII）
# ---------------------------------------------------------------------------
_PS_PRE = "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
_PS_CATCH = "[Console]::Error.WriteLine(\"RRUN-ERROR $($_.Exception.Message)\");exit 1"

# dest/base → 最终落点（~ 展开、目录目标补 basename、自动建父目录）
_PS_RESOLVE = (
    "if($dest -eq '~'){$dest=$HOME}"
    "elseif($dest.StartsWith('~/') -or $dest.StartsWith('~\\')){$dest=Join-Path $HOME $dest.Substring(2)};"
    "if($dest.EndsWith('/') -or $dest.EndsWith('\\')){"
    "$dest=$dest.TrimEnd('/','\\');"
    "if(-not (Test-Path -LiteralPath $dest -PathType Container))"
    "{New-Item -ItemType Directory -Force -Path $dest|Out-Null};"
    "$dest=Join-Path $dest $base"
    "}elseif(Test-Path -LiteralPath $dest -PathType Container){$dest=Join-Path $dest $base};"
    "$parent=Split-Path -Parent $dest;"
    "if($parent -and -not (Test-Path -LiteralPath $parent))"
    "{New-Item -ItemType Directory -Force -Path $parent|Out-Null};"
)

# 校验 tmp（$wantSize/$wantSha 为 -1/'' 时跳过对应项），不符删 tmp 后 exit 3
_PS_VERIFY = (
    "$size=[long](Get-Item -LiteralPath $tmp).Length;"
    "if($wantSize -ge 0 -and $size -ne $wantSize){"
    "Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue;"
    "[Console]::Error.WriteLine(\"RRUN-ERROR size mismatch: expected $wantSize, got $size\");exit 3};"
    "$sha=(Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower();"
    "if($wantSha -and $sha -ne $wantSha){"
    "Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue;"
    "[Console]::Error.WriteLine(\"RRUN-ERROR sha256 mismatch: expected $wantSha, got $sha\");exit 3};"
)

_PS_FINALIZE = _PS_VERIFY + (
    "Move-Item -Force -LiteralPath $tmp -Destination $dest;"
    "[Console]::Error.WriteLine(\"RRUN $size $sha\");exit 0"
)


@dataclass
class TransferResult:
    host: str
    ip: str
    op: str               # push | pull
    local_path: str
    remote_path: str
    size: int             # 实际传输字节数（-1 = 未知/失败）
    sha256: str
    exit_code: int
    duration: float
    verified: bool = False         # 两端 sha256 一致
    transport_error: bool = False  # ssh 层失败（exit 255）
    timed_out: bool = False
    message: str = ""              # 远端 RRUN-ERROR 或本地错误说明


# ---------------------------------------------------------------------------
# 纯函数（命令组装 / 路径 / 回执解析）
# ---------------------------------------------------------------------------

def _b64utf8(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _ps_utf8_var(name: str, value: str) -> str:
    """PS 变量赋值，值经 base64(utf-8) 内嵌——单行命令保持纯 ASCII，CJK 免疫。"""
    return f"${name}=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64utf8(value)}'));"


def _wrap_ps(body: str) -> str:
    """try/catch 包裹为单行命令，并断言单行 + 纯 ASCII（-Command - 协议约束）。"""
    line = _PS_PRE + "try{" + body + "}catch{" + _PS_CATCH + "}"
    assert "\n" not in line, "powershell oneliner 必须单行"
    line.encode("ascii")  # 必须纯 ASCII
    return line


def build_posix_push_command(machine: Machine, dest: str, base: str, size: int, sha: str) -> str:
    argv = [dest, base, str(int(size)), sha]
    return "bash -c " + shlex.quote(_POSIX_PUSH_SCRIPT) + " -- " + " ".join(shlex.quote(a) for a in argv)


def build_posix_pull_command(machine: Machine, src: str) -> str:
    return "bash -c " + shlex.quote(_POSIX_PULL_SCRIPT) + " -- " + shlex.quote(src)


def build_win_push_single(dest: str, base: str, size: int, sha: str, token: str, data: bytes) -> str:
    """单块一次成形：resolve → 写 tmp → 校验 → rename → 回执。"""
    body = (
        _ps_utf8_var("dest", dest) + _ps_utf8_var("base", base)
        + f"$wantSize={int(size)};$wantSha='{sha}';"
        + _PS_RESOLVE
        + f"$tmp=\"$dest.rrun-tmp-{token}\";"
        + f"$b=[Convert]::FromBase64String('{base64.b64encode(data).decode('ascii')}');"
        + "[IO.File]::WriteAllBytes($tmp,$b);"
        + _PS_FINALIZE
    )
    return _wrap_ps(body)


def build_win_push_init(dest: str, base: str, token: str) -> str:
    """分块上传首轮：resolve + 清理陈旧 tmp + 回传解析后的 dest（base64）。"""
    body = (
        _ps_utf8_var("dest", dest) + _ps_utf8_var("base", base)
        + _PS_RESOLVE
        + f"$tmp=\"$dest.rrun-tmp-{token}\";"
        + "if(Test-Path -LiteralPath $tmp){Remove-Item -Force -LiteralPath $tmp};"
        + "[Console]::Error.WriteLine('RRUN-DEST-B64 '"
        + "+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dest)));exit 0"
    )
    return _wrap_ps(body)


def build_win_push_append(tmp: str, data: bytes) -> str:
    body = (
        _ps_utf8_var("tmp", tmp)
        + f"$b=[Convert]::FromBase64String('{base64.b64encode(data).decode('ascii')}');"
        + "$fs=[IO.File]::Open($tmp,[IO.FileMode]::Append,[IO.FileAccess]::Write,[IO.FileShare]::None);"
        + "$fs.Write($b,0,$b.Length);$fs.Close();exit 0"
    )
    return _wrap_ps(body)


def build_win_push_finalize(dest: str, tmp: str, size: int, sha: str) -> str:
    body = (
        _ps_utf8_var("dest", dest) + _ps_utf8_var("tmp", tmp)
        + f"$wantSize={int(size)};$wantSha='{sha}';"
        + _PS_FINALIZE
    )
    return _wrap_ps(body)


def build_win_pull_command(src: str) -> str:
    script = _ps_utf8_var("src", src).rstrip(";") + "\n" + _PS_PULL_BODY
    return ("powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
            + base64.b64encode(script.encode("utf-16-le")).decode("ascii"))


def _remote_basename(path: str, is_windows: bool) -> str:
    """远端路径的 basename（按远端 OS 的路径分隔符解析）。"""
    if is_windows:
        return PureWindowsPath(path).name
    return PurePosixPath(path).name


def _resolve_local_dest(local: str, remote_base: str) -> Path:
    """pull 本地落点：已存在目录或以分隔符结尾 → 保留远端 basename。"""
    p = Path(local).expanduser()
    if local.endswith(("/", "\\")) or p.is_dir():
        return p / remote_base
    return p


_RECEIPT_RE = re.compile(r"^RRUN (\d+) ([0-9a-f]{64})$")
_ERROR_RE = re.compile(r"^RRUN-ERROR (.*)$")
_DEST_RE = re.compile(r"^RRUN-DEST-B64 ([A-Za-z0-9+/=]+)$")


def _parse_stderr(stderr: bytes) -> "tuple[int, str, str]":
    """从远端 stderr 提取回执/错误，返回 (size, sha256, error)。噪声行忽略。"""
    size, sha, error = -1, "", ""
    for line in stderr.decode("utf-8", "replace").splitlines():
        line = line.strip()
        m = _RECEIPT_RE.match(line)
        if m:
            size, sha = int(m.group(1)), m.group(2)
            continue
        m = _ERROR_RE.match(line)
        if m:
            error = m.group(1)
    return size, sha, error


def _parse_dest(stderr: bytes) -> str:
    """从 init 回执解析解析后的远端 dest（base64 回传，绕 CJK/编码）。"""
    for line in stderr.decode("utf-8", "replace").splitlines():
        m = _DEST_RE.match(line.strip())
        if m:
            return base64.b64decode(m.group(1)).decode("utf-8")
    return ""


# ---------------------------------------------------------------------------
# 传输执行
# ---------------------------------------------------------------------------

def _heal_mux(machine: Machine) -> None:
    """超时杀进程后自愈：被杀的 mux 通道会把 ControlMaster 主连接打入 wedge 状态
    （实测：后续所有复用该连接的 exec/push 全部挂起直至 ControlPersist 过期）。
    主动关闭主连接，下次操作重建。失败静默。"""
    try:
        from .executor import close_mux
        close_mux(machine.name)
    except Exception:  # noqa: BLE001 - 自愈尽力而为
        pass


def _arm_watchdog(p: subprocess.Popen, timeout: "float | None", fired: list) -> "threading.Timer | None":
    """超时看门狗：到点杀 ssh 进程并置 fired 标记（流式读写在 Popen 上无法直接 wait(timeout)）。"""
    if not timeout or timeout <= 0:
        return None

    def _kill():
        fired.append(1)
        try:
            p.kill()
        except OSError:
            pass

    t = threading.Timer(timeout, _kill)
    t.daemon = True
    t.start()
    return t


def _win_call(machine: Machine, oneliner: str, deadline: "float | None",
              mux: bool) -> "tuple[int, bytes, bool]":
    """单次 -Command - 调用：oneliner（单行 ASCII）经 stdin 下发；返回 (rc, stderr, timed_out)。"""
    remaining = None if deadline is None else max(0.1, deadline - time.time())
    rc, _out, err, timed_out = _ssh_run(machine, PS_REMOTE_CMD,
                                        (oneliner + "\n").encode("ascii"), remaining, mux)
    return (EXIT_TIMEOUT if timed_out else rc), err, timed_out


def _push_windows(machine: Machine, src_fh, remote: str, base: str,
                  want_size: int, want_sha: str, deadline: "float | None",
                  mux: bool) -> "tuple[int, int, str, str, bool]":
    """返回 (rc, size, sha256, message, timed_out)。分块追加协议见模块 docstring。"""
    token = secrets.token_hex(4)
    h = hashlib.sha256()
    total = 0
    timed_out = False

    first = src_fh.read(_WIN_CHUNK)
    second = src_fh.read(_WIN_CHUNK)
    if not second:  # 单块（含空文件）一次成形
        h.update(first)
        total = len(first)
        # 文件源用预计算值；stdin 源此刻才算得出
        size_arg = want_size if want_size >= 0 else total
        sha_arg = want_sha or h.hexdigest()
        rc, err, timed_out = _win_call(
            machine, build_win_push_single(remote, base, size_arg, sha_arg, token, first),
            deadline, mux)
        rsize, rsha, error = _parse_stderr(err)
        return rc, rsize, rsha, error, timed_out

    # 多块：init → append×N → finalize
    rc, err, timed_out = _win_call(machine, build_win_push_init(remote, base, token), deadline, mux)
    dest = _parse_dest(err)
    if timed_out or rc != 0 or not dest:
        _s, _h, error = _parse_stderr(err)
        return (rc if rc != 0 else 1), -1, "", error or "push init failed", timed_out
    tmp = f"{dest}.rrun-tmp-{token}"

    for chunk in (first, second):
        h.update(chunk)
        total += len(chunk)
        rc, err, timed_out = _win_call(machine, build_win_push_append(tmp, chunk), deadline, mux)
        if timed_out or rc != 0:
            _s, _h, error = _parse_stderr(err)
            return (rc if rc != 0 else 1), -1, "", error or "push append failed", timed_out
    while True:
        chunk = src_fh.read(_WIN_CHUNK)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
        rc, err, timed_out = _win_call(machine, build_win_push_append(tmp, chunk), deadline, mux)
        if timed_out or rc != 0:
            _s, _h, error = _parse_stderr(err)
            return (rc if rc != 0 else 1), -1, "", error or "push append failed", timed_out

    size_arg = want_size if want_size >= 0 else total
    sha_arg = want_sha or h.hexdigest()
    rc, err, timed_out = _win_call(
        machine, build_win_push_finalize(dest, tmp, size_arg, sha_arg), deadline, mux)
    rsize, rsha, error = _parse_stderr(err)
    return rc, rsize, rsha, error, timed_out


def push(host: str, local: str, remote: str, timeout: "float | None" = None,
         mux: bool = True, audit: bool = True) -> TransferResult:
    """推本地文件（或 stdin，local="-"）到远端。原子替换，sha256 校验。"""
    machine = resolve_machine(host)
    t0 = time.time()
    deadline = t0 + timeout if timeout and timeout > 0 else None

    want_size, want_sha, local_path = -1, "", local
    if local == "-":
        src_fh = sys.stdin.buffer
        base = ""
        local_path = "<stdin>"
        if remote.endswith(("/", "\\")):
            raise ValueError("push from stdin needs a remote file path, not a directory")
    else:
        src_path = Path(local).expanduser()
        if src_path.is_dir():
            raise ValueError(f"local path is a directory (recursive transfer not supported): {local}")
        if not src_path.is_file():
            raise ValueError(f"local file not found: {local}")
        h = hashlib.sha256()
        with open(src_path, "rb") as f:
            for chunk in iter(lambda: f.read(_IO_CHUNK), b""):
                h.update(chunk)
        want_size = src_path.stat().st_size
        want_sha = h.hexdigest()
        src_fh = open(src_path, "rb")
        base = src_path.name

    try:
        if machine.is_windows:
            rc, size, sha, error, timed_out = _push_windows(
                machine, src_fh, remote, base, want_size, want_sha, deadline, mux)
        else:
            rc, size, sha, error, timed_out = _push_posix(
                machine, src_fh, remote, base, want_size, want_sha, timeout, mux)
    finally:
        if local != "-":
            src_fh.close()
    if timed_out and mux:
        _heal_mux(machine)
    duration = time.time() - t0

    # 回执与本地计算交叉校验（双保险；stdin 源在 POSIX 下远端已 rename，不符会提示重推）
    verified = bool(sha) and (not want_sha or sha == want_sha)
    if rc == 0 and not verified:
        rc = EXIT_INTEGRITY
        error = error or ("integrity mismatch: remote receipt does not match local sha256 "
                          "(remote file already renamed — suspect, re-push)")
    result = TransferResult(
        host=machine.name, ip=machine.ip, op="push", local_path=local_path,
        remote_path=remote, size=size if size >= 0 else want_size, sha256=sha or want_sha,
        exit_code=rc, duration=duration, verified=verified,
        transport_error=(rc == EXIT_TRANSPORT_ERROR), timed_out=timed_out,
        message=error,
    )
    if audit:
        _write_audit(result)
    return result


def _push_posix(machine: Machine, src_fh, remote: str, base: str,
                want_size: int, want_sha: str, timeout: "float | None",
                mux: bool) -> "tuple[int, int, str, str, bool]":
    """裸二进制 stdin 直推，EOF 收尾。返回 (rc, size, sha256, message, timed_out)。"""
    remote_cmd = build_posix_push_command(machine, remote, base, want_size, want_sha)
    p = subprocess.Popen(
        [*_ssh_args(machine, mux), machine.target, remote_cmd],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    fired: list = []
    watchdog = _arm_watchdog(p, timeout, fired)
    try:
        for chunk in iter(lambda: src_fh.read(_IO_CHUNK), b""):
            p.stdin.write(chunk)
        p.stdin.close()
    except (BrokenPipeError, ConnectionResetError, OSError):
        try:
            p.stdin.close()
        except OSError:
            pass
    finally:
        p.stdin = None  # 防 communicate 二次 flush
    _out, err = p.communicate()
    if watchdog:
        watchdog.cancel()
    rc = EXIT_TIMEOUT if fired else p.returncode
    size, sha, error = _parse_stderr(err)
    return rc, size, sha, error, bool(fired)


def pull(host: str, remote: str, local: str, timeout: "float | None" = None,
         mux: bool = True, audit: bool = True) -> TransferResult:
    """从远端拉文件到本地（或 stdout，local="-"）。原子替换，sha256 校验。"""
    machine = resolve_machine(host)
    t0 = time.time()

    remote_base = _remote_basename(remote, machine.is_windows)
    to_stdout = local == "-"
    dest = None
    tmp_path = None
    local_path = "<stdout>"
    if not to_stdout:
        dest = _resolve_local_dest(local, remote_base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.parent / f"{dest.name}.rrun-tmp-{os.getpid()}"
        local_path = str(dest)

    if machine.is_windows:
        remote_cmd = build_win_pull_command(remote)
    else:
        remote_cmd = build_posix_pull_command(machine, remote)
    p = subprocess.Popen(
        [*_ssh_args(machine, mux), machine.target, remote_cmd],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    fired: list = []
    watchdog = _arm_watchdog(p, timeout, fired)

    h = hashlib.sha256()
    size = 0
    tmp_fh = None if to_stdout else open(tmp_path, "wb")
    sink = sys.stdout.buffer if to_stdout else tmp_fh
    try:
        while True:
            chunk = p.stdout.read(_IO_CHUNK)
            if not chunk:
                break
            sink.write(chunk)
            h.update(chunk)
            size += len(chunk)
        sink.flush()
    finally:
        if tmp_fh is not None:
            tmp_fh.close()
    p.wait()
    if watchdog:
        watchdog.cancel()
    err = p.stderr.read()
    rc = EXIT_TIMEOUT if fired else p.returncode
    duration = time.time() - t0
    if fired and mux:
        _heal_mux(machine)

    rsize, rsha, error = _parse_stderr(err)
    local_sha = h.hexdigest()
    verified = bool(rsha) and rsha == local_sha and rsize == size

    if rc == 0 and not verified:
        rc = EXIT_INTEGRITY
        error = error or f"integrity mismatch: remote sha256 {rsha or '<none>'} != local {local_sha}"
    if tmp_path is not None:
        if rc == 0 and verified:
            os.replace(tmp_path, dest)
        else:
            tmp_path.unlink(missing_ok=True)

    result = TransferResult(
        host=machine.name, ip=machine.ip, op="pull", local_path=local_path,
        remote_path=remote, size=size, sha256=local_sha,
        exit_code=rc, duration=duration, verified=verified,
        transport_error=(rc == EXIT_TRANSPORT_ERROR), timed_out=bool(fired),
        message=error,
    )
    if audit:
        _write_audit(result)
    return result


def _write_audit(result: TransferResult) -> None:
    """传输审计：与 exec 同一本 jsonl，op 字段区分。"""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "op": result.op,
            "host": result.host, "ip": result.ip,
            "local": result.local_path, "remote": result.remote_path,
            "size": result.size, "sha256": result.sha256,
            "verified": result.verified,
            "exit_code": result.exit_code,
            "duration_s": round(result.duration, 2),
            "transport_error": result.transport_error, "timed_out": result.timed_out,
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 审计失败不阻断传输
