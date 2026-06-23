"""验证 PowerShell UTF-8 编码前缀修复中文乱码问题。

运行方式：python tmp/verify_ps_utf8.py
"""

import subprocess
import shutil
import sys
import os

# 强制 Python 进程自身使用 UTF-8 输出，避免 print 中文时崩溃
os.environ["PYTHONUTF8"] = "1"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 测试用的中文字符串 ──
CHINESE_TEXT = "你好世界！测试中文输出。"


def run_ps(command: str, with_utf8_preamble: bool) -> tuple[int, str, str]:
    """用 PowerShell 执行命令，可选是否注入 UTF-8 前缀。"""
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        print("ERROR: PowerShell 未找到，无法运行测试")
        sys.exit(1)

    if with_utf8_preamble:
        preamble = (
            "chcp 65001 | Out-Null; "
            "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        )
        full_command = preamble + command
    else:
        full_command = command

    argv = [executable, "-NoProfile", "-NonInteractive", "-Command", full_command]

    # 与 tools.py 中 _run_shell_process 保持一致的参数
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    stdout, stderr = proc.communicate(timeout=10)
    return proc.returncode, stdout, stderr


def safe_repr(text: str, max_len: int = 80) -> str:
    """安全地展示可能含乱码的文本，替换无法编码的字符。"""
    truncated = text.strip()[:max_len]
    return truncated


def contains_chinese(output: str, expected: str) -> bool:
    """检查输出中是否包含预期的中文字符。"""
    return expected in output


def is_garbled(text: str) -> bool:
    """粗略判断文本是否包含乱码特征（大量替换字符 U+FFFD）。"""
    return "�" in text


def main():
    print("=" * 60)
    print("PowerShell UTF-8 编码修复验证")
    print("=" * 60)

    # ── 测试 1: Write-Output 直接输出中文 ──
    print("\n[测试 1] Write-Output 中文输出")
    print(f"  期望输出包含: {CHINESE_TEXT}")

    code, out_no, err_no = run_ps(f"Write-Output '{CHINESE_TEXT}'", with_utf8_preamble=False)
    code, out_yes, err_yes = run_ps(f"Write-Output '{CHINESE_TEXT}'", with_utf8_preamble=True)

    print(f"  无前缀输出: {safe_repr(out_no)}")
    print(f"  有前缀输出: {safe_repr(out_yes)}")
    no_ok = contains_chinese(out_no, CHINESE_TEXT)
    yes_ok = contains_chinese(out_yes, CHINESE_TEXT)
    no_garbled = is_garbled(out_no)
    yes_garbled = is_garbled(out_yes)
    print(f"  无前缀匹配中文: {no_ok}  出现乱码: {no_garbled}")
    print(f"  有前缀匹配中文: {yes_ok}  出现乱码: {yes_garbled}")

    # ── 测试 2: PowerShell 错误信息（含中文） ──
    print("\n[测试 2] PowerShell 中文错误信息")
    ps_error_cmd = "Get-Item '不存在的路径xyz' -ErrorAction Continue"
    code, out_no, err_no = run_ps(ps_error_cmd, with_utf8_preamble=False)
    code, out_yes, err_yes = run_ps(ps_error_cmd, with_utf8_preamble=True)

    combined_no = out_no + err_no
    combined_yes = out_yes + err_yes

    has_chinese_kw_no = any(kw in combined_no for kw in ["找不到", "不存在", "路径", "项目"])
    has_chinese_kw_yes = any(kw in combined_yes for kw in ["找不到", "不存在", "路径", "项目"])
    no_garbled2 = is_garbled(combined_no)
    yes_garbled2 = is_garbled(combined_yes)

    print(f"  无前缀 stderr: {safe_repr(err_no, 120)}")
    print(f"  有前缀 stderr: {safe_repr(err_yes, 120)}")
    print(f"  无前缀含中文关键词: {has_chinese_kw_no}  出现乱码: {no_garbled2}")
    print(f"  有前缀含中文关键词: {has_chinese_kw_yes}  出现乱码: {yes_garbled2}")

    # ── 测试 3: Python 子进程输出中文 ──
    print("\n[测试 3] Python print 中文输出")
    py_cmd = f'python -c "print(\'{CHINESE_TEXT}\')"'
    code, out_no, err_no = run_ps(py_cmd, with_utf8_preamble=False)
    code, out_yes, err_yes = run_ps(py_cmd, with_utf8_preamble=True)

    print(f"  无前缀输出: {safe_repr(out_no)}")
    print(f"  有前缀输出: {safe_repr(out_yes)}")
    py_no_ok = contains_chinese(out_no, CHINESE_TEXT)
    py_yes_ok = contains_chinese(out_yes, CHINESE_TEXT)
    py_no_garbled = is_garbled(out_no)
    py_yes_garbled = is_garbled(out_yes)
    print(f"  无前缀匹配中文: {py_no_ok}  出现乱码: {py_no_garbled}")
    print(f"  有前缀匹配中文: {py_yes_ok}  出现乱码: {py_yes_garbled}")

    # ── 总结 ──
    print("\n" + "=" * 60)
    print("验证总结")
    print("=" * 60)

    all_yes_ok = yes_ok and py_yes_ok and not yes_garbled and not yes_garbled2 and not py_yes_garbled
    any_no_garbled = no_garbled or no_garbled2 or py_no_garbled or not no_ok or not py_no_ok

    if all_yes_ok:
        print("PASS: UTF-8 前缀修复有效，中文输出不再乱码")
    else:
        print("FAIL: UTF-8 前缀未能完全解决乱码，需进一步排查")

    if any_no_garbled:
        print("INFO: 无前缀时中文输出确实出现乱码，说明问题是真实存在的")
    else:
        print("INFO: 当前系统可能已默认 UTF-8，但仍建议保留前缀以确保兼容性")


if __name__ == "__main__":
    main()
