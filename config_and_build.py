#!/usr/bin/env python3
"""
HarmonyOS 工程配置 → 编译 → 部署 → 启动 一体脚本。

- 从配置文件读取 signingConfigs / bundleName
- 写入 AppScope/app.json5（bundleName）和 build-profile.json5（signingConfigs）
- 调用 hvigorw assembleHap 编译
- 查找 *-signed.hap，hdc install 到设备并校验安装结果
- hdc shell aa start 启动应用

用法：
  python config_and_build.py --project D:/MyHarmonyApp
  python config_and_build.py --project D:/MyHarmonyApp --dry-run
  python config_and_build.py --yaml ./other-config.yaml --project D:/MyHarmonyApp

环境变量（可选，未设置时从 PATH 查找）：
  HVIGOR_HOME    hvigor 安装根目录（如 D:/Huawei/DevEcoStudio/tools/hvigor）
  HDC_PATH       hdc 可执行文件路径
  NODE_HOME      Node.js 安装根目录
  JAVA_HOME      JDK 安装根目录
  OHPM_HOME      ohpm 安装根目录（如 D:/Huawei/DevEcoStudio/tools/ohpm）
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# =============================================================================
# 工具路径解析
# =============================================================================


def _find_tool(name: str, env_var: str, *extra_dirs: str) -> str | None:
    """依次查：环境变量 → PATH → 候选目录。返回可执行文件完整路径。"""
    # 1) 环境变量
    env_val = os.environ.get(env_var)
    if env_val:
        expanded = os.path.expandvars(env_val)
        # 可能是文件
        if os.path.isfile(expanded):
            return os.path.normpath(expanded)
        # 可能是目录，进目录找
        if os.path.isdir(expanded):
            p = os.path.join(expanded, name)
            if os.path.isfile(p):
                return os.path.normpath(p)

    # 2) PATH
    found = shutil.which(name)
    if found:
        return os.path.normpath(found)

    # 3) 候选目录
    for d in extra_dirs:
        d = os.path.expandvars(d)
        p = os.path.join(d, name) if not d.endswith(name) else d
        if os.path.isfile(p):
            return os.path.normpath(p)

    return None


def resolve_tools() -> dict[str, str]:
    """返回 {hdc, hvigorw, node, java, ohpm} 路径字典。"""
    is_win = sys.platform == "win32"
    hdc_name = "hdc.exe" if is_win else "hdc"
    hvigorw_name = "hvigorw.bat" if is_win else "hvigorw"
    node_name = "node.exe" if is_win else "node"
    java_name = "java.exe" if is_win else "java"
    ohpm_name = "ohpm.bat" if is_win else "ohpm"

    tools = {
        "hdc": _find_tool(hdc_name, "HDC_PATH") or hdc_name,
        "hvigorw": _find_tool(hvigorw_name, "HVIGOR_HOME", "${HVIGOR_HOME}/bin")
        or hvigorw_name,
    }
    node = _find_tool(node_name, "NODE_HOME", "${NODE_HOME}/bin")
    if node:
        tools["node"] = node
    java = _find_tool(java_name, "JAVA_HOME", "${JAVA_HOME}/bin")
    if java:
        tools["java"] = java
    ohpm = _find_tool(ohpm_name, "OHPM_HOME", "${OHPM_HOME}/bin") or shutil.which(
        "ohpm"
    )
    if ohpm:
        tools["ohpm"] = ohpm
    return tools


# =============================================================================
# 配置文件加载（无需 pyyaml，纯内置库解析）
# =============================================================================


def _extract_json_array(text: str, key: str) -> list:
    """从类 YAML 文本中提取 key 对应的 JSON 数组值。"""
    m = re.search(rf"{re.escape(key)}\s*:\s*\[", text)
    if not m:
        sys.exit(f"[ERROR] 配置缺少字段: {key}")
    start = m.end() - 1  # '[' 的位置
    depth, end, in_str, escape = 1, start + 1, False, False
    while end < len(text) and depth > 0:
        c = text[end]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
        end += 1
    if depth != 0:
        sys.exit(f"[ERROR] {key} 的 JSON 数组括号不匹配")
    return json.loads(text[start:end])


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"[ERROR] 配置文件不存在: {path}")
    with open(p, "r", encoding="utf-8") as f:
        text = f.read()

    # 提取 signingConfigs（JSON 数组）
    signing_configs = _extract_json_array(text, "signingConfigs")

    # 提取 bundleName（支持引号包裹或裸字符串）
    m = re.search(r"""bundleName\s*:\s*["']?([^"'\s#]+)""", text)
    if not m:
        sys.exit("[ERROR] 配置缺少字段: bundleName")
    bundle_name = m.group(1)

    return {"signingConfigs": signing_configs, "bundleName": bundle_name}


# =============================================================================
# JSON5 原地编辑（保格式，不破坏注释/尾逗号）
# =============================================================================


def _find_value_range(text: str, key: str, start: int = 0) -> tuple[int, int] | None:
    """在 JSON5 文本中定位 key 对应值的起止位置 (value_start, value_end)。"""
    pattern = re.compile(rf'["\']?{re.escape(key)}["\']?\s*:\s*', re.M)
    m = pattern.search(text, start)
    if not m:
        return None

    pos = m.end()
    ch = text[pos]

    # 字符串
    if ch in ('"', "'"):
        quote = ch
        i = pos + 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == quote:
                return (pos, i)
            i += 1
        return None

    # 对象 {}
    if ch == "{":
        depth = 1
        i = pos + 1
        in_str = False
        sq = ""
        while i < len(text) and depth > 0:
            c = text[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == sq:
                    in_str = False
                i += 1
                continue
            if c in ('"', "'"):
                in_str = True
                sq = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        return (pos, i - 1)

    # 数组 []
    if ch == "[":
        depth = 1
        i = pos + 1
        in_str = False
        sq = ""
        while i < len(text) and depth > 0:
            c = text[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == sq:
                    in_str = False
                i += 1
                continue
            if c in ('"', "'"):
                in_str = True
                sq = c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
            i += 1
        return (pos, i - 1)

    # 数字 / 布尔 / null
    m2 = re.match(r"[\w.+\-]+", text[pos:])
    if m2:
        return (pos, pos + m2.end() - 1)
    return None


def set_json5(
    file_path: str, key: str, new_value_text: str, parent_key: str | None = None
) -> None:
    """原地替换 JSON5 文件中 key 对应的值。

    parent_key: 如果 key 嵌套在某个父 key 下，先定位父 key 的值范围，再在其内查找 key。
                例如 set_json5("build-profile.json5", "signingConfigs", ..., "app")
    """
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    search_start = 0
    if parent_key:
        parent_range = _find_value_range(content, parent_key)
        if parent_range is None:
            sys.exit(f"[ERROR] 在 {file_path} 中未找到父 key: {parent_key}")
        search_start = parent_range[0] + 1  # 跳过开头 {
        # 限制搜索范围不超过父对象结尾
        parent_end = parent_range[1]
        # 在父对象范围内搜索
        rng = _find_value_range(content, key, search_start)
        if rng is None or rng[1] > parent_end:
            sys.exit(f"[ERROR] 在 {file_path} 的 {parent_key} 内未找到 key: {key}")
    else:
        rng = _find_value_range(content, key)
        if rng is None:
            sys.exit(f"[ERROR] 在 {file_path} 中未找到 key: {key}")

    lo, hi = rng
    new_content = content[:lo] + new_value_text + content[hi + 1 :]
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"  [OK] 已更新 {os.path.basename(file_path)} 中的 {key}")


# =============================================================================
# 配置步骤
# =============================================================================


def _strip_json5_comments(text: str) -> str:
    """移除 JSON5 中的单行 // 和多行 块注释，返回纯 JSON 字符串。"""
    # 移除 // 行注释
    result = re.sub(r"//[^\n]*", "", text)
    # 移除块注释
    result = re.sub(r"/\*.*?\*/", "", result, flags=re.DOTALL)
    return result


def _clean_json5_for_parsing(text: str) -> str:
    """将 JSON5 文本转为可被 json.loads 解析的标准 JSON 字符串。"""
    s = _strip_json5_comments(text)
    # 移除尾随逗号（}, 或 ], 前的逗号）
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def configure_app(project_dir: str, bundle_name: str) -> None:
    """配置 AppScope/app.json5：更新 bundleName 并补齐必填字段。"""
    app_json5 = os.path.join(project_dir, "AppScope", "app.json5")
    if not os.path.exists(app_json5):
        sys.exit(f"[ERROR] 未找到 AppScope/app.json5: {app_json5}")
    print(f"\n>>> [1/6] 配置 app.json5")

    with open(app_json5, "r", encoding="utf-8") as f:
        content = f.read()

    # 找到 app 对象的起止位置
    app_range = _find_value_range(content, "app")
    if app_range is None:
        sys.exit(f"[ERROR] 在 app.json5 中未找到 app 字段")

    app_start, app_end = app_range
    raw_app = content[app_start : app_end + 1]

    # 尝试解析 app 对象
    try:
        app_obj = json.loads(_clean_json5_for_parsing(raw_app))
    except json.JSONDecodeError as e:
        sys.exit(
            f"[ERROR] 无法解析 app.json5 中的 app 对象: {e}\n内容: {raw_app[:200]}"
        )

    # 更新 bundleName
    app_obj["bundleName"] = bundle_name
    print(f"  bundleName → {bundle_name}")

    # 补齐必填字段（HarmonyOS app.json5 schema 要求）
    defaults = {
        "label": "$string:app_name",
        "icon": "$media:app_icon",
        "vendor": "example",
        "versionCode": 1000000,
        "versionName": "1.0.0",
    }
    for key, default in defaults.items():
        if key not in app_obj:
            app_obj[key] = default
            print(f"  [补] {key} → {default}")

    # 序列化回 JSON5 友好格式（compact，不换行保持风格一致）
    # 检查原文件是否是多行格式
    is_multiline = "\n" in raw_app and raw_app.strip().startswith("{")
    indent = 2 if is_multiline else None
    new_app = json.dumps(app_obj, indent=indent, ensure_ascii=False)

    new_content = content[:app_start] + new_app + content[app_end + 1 :]
    with open(app_json5, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"  [OK] 已更新 {os.path.basename(app_json5)}")


def configure_signing(project_dir: str, signing_configs: list) -> None:
    build_json5 = os.path.join(project_dir, "build-profile.json5")
    if not os.path.exists(build_json5):
        sys.exit(f"[ERROR] 未找到 build-profile.json5: {build_json5}")
    print(f"\n>>> [2/6] 配置 signingConfigs")
    configs_text = json.dumps(signing_configs, indent=2, ensure_ascii=False)
    set_json5(build_json5, "signingConfigs", configs_text, parent_key="app")


# =============================================================================
# 编译步骤
# =============================================================================


def build(
    project_dir: str,
    hvigorw: str,
    node_path: str | None,
    java_home: str | None,
    ohpm_path: str | None,
    dry_run: bool,
) -> bool:
    print(f"\n>>> [3/6] 编译工程")

    # 解析 hvigorw 路径
    if os.path.isabs(hvigorw) and os.path.isfile(hvigorw):
        hvigorw_cmd = hvigorw
    elif os.path.isfile(os.path.join(project_dir, hvigorw)):
        hvigorw_cmd = os.path.join(project_dir, hvigorw)
    else:
        for name in ("hvigorw.bat", "hvigorw"):
            p = os.path.join(project_dir, name)
            if os.path.isfile(p):
                hvigorw_cmd = p
                break
        else:
            sys.exit(f"[ERROR] 未找到 hvigorw 构建脚本，请设置 HVIGOR_HOME")

    print(f"  hvigorw: {hvigorw_cmd}")

    # 环境变量
    env = os.environ.copy()
    if node_path:
        env["PATH"] = os.pathsep.join([os.path.dirname(node_path), env["PATH"]])
        print(f"  NODE:    {node_path}")
    if java_home:
        env["JAVA_HOME"] = java_home
        print(f"  JAVA:    {java_home}")
    if ohpm_path:
        env["PATH"] = os.pathsep.join([os.path.dirname(ohpm_path), env["PATH"]])
        print(f"  OHPM:    {ohpm_path}")

    # --- 3a. ohpm install ---
    ohpm_cmd = ohpm_path or shutil.which("ohpm") or "ohpm"
    print(f"  执行 ohpm install ...")
    if not dry_run:
        result = subprocess.run(
            [ohpm_cmd, "install"],
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            print("[WARN] ohpm install 返回非零，尝试继续编译")
        else:
            print("  [OK] ohpm install 完成")

    # --- 3b. hvigor assembleHap ---
    cmd = (
        ["cmd", "/c", hvigorw_cmd, "assembleHap"]
        if hvigorw_cmd.endswith(".bat")
        else [hvigorw_cmd, "assembleHap"]
    )
    print(f"  执行: {' '.join(cmd)}")

    if dry_run:
        print("  [DRY-RUN] 跳过 hvigor 编译")
        return True

    try:
        proc = subprocess.run(
            cmd,
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("[ERROR] 编译超时（>10 分钟）")
        return False

    if proc.returncode != 0:
        print(f"[ERROR] 编译失败 (exit={proc.returncode})")
        output = (proc.stdout or "") + (proc.stderr or "")
        for line in output.split("\n")[-30:]:
            if line.strip():
                print(f"  | {line.strip()}")
        return False

    print("  [OK] 编译成功")
    return True


# =============================================================================
# 查找 HAP
# =============================================================================


def find_hap(project_dir: str) -> str | None:
    print(f"\n>>> [4/6] 查找签名 HAP")
    candidates: list[str] = []
    for root, _, files in os.walk(project_dir):
        for fn in files:
            if fn.endswith("-signed.hap"):
                candidates.append(os.path.join(root, fn))
    if not candidates:
        for root, _, files in os.walk(project_dir):
            for fn in files:
                if fn.endswith(".hap"):
                    candidates.append(os.path.join(root, fn))

    if not candidates:
        print("[ERROR] 未找到 .hap 文件")
        return None

    candidates.sort(key=os.path.getmtime, reverse=True)
    hap = candidates[0]
    print(f"  找到: {hap}")
    return hap


# =============================================================================
# 安装 & 启动
# =============================================================================


def _print_output_tail(output: str, lines: int = 10) -> None:
    for line in output.split("\n")[-lines:]:
        if line.strip():
            print(f"  | {line.strip()}")


def _is_existing_bundle_install_error(output: str, bundle_name: str) -> bool:
    lower = output.lower()
    conflict_keywords = (
        "already",
        "exist",
        "exists",
        "installed",
        "signature",
        "sign",
        "same",
        "conflict",
        "duplicate",
        "已存在",
        "已安装",
        "签名",
        "同包名",
        "包名",
        "冲突",
    )
    return bundle_name.lower() in lower or any(k in lower for k in conflict_keywords)


def _bundle_exists_on_device(hdc: str, bundle_name: str) -> bool:
    try:
        r = subprocess.run(
            [hdc, "shell", "bm", "dump", "-n", bundle_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return False

    output = (r.stdout or "") + (r.stderr or "")
    lower = output.lower()
    missing_markers = (
        "not exist",
        "not exists",
        "not found",
        "does not exist",
        "failed",
        "error",
        "不存在",
        "未找到",
        "失败",
    )
    if r.returncode != 0 or any(marker in lower for marker in missing_markers):
        return False
    return bundle_name.lower() in lower or "ability:" in lower


def _wait_for_bundle_on_device(
    hdc: str, bundle_name: str, attempts: int = 6, delay: float = 2.0
) -> bool:
    for i in range(attempts):
        if _bundle_exists_on_device(hdc, bundle_name):
            return True
        if i < attempts - 1:
            time.sleep(delay)
    return False


def _run_install(hdc: str, hap_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [hdc, "install", "-r", hap_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _run_uninstall(hdc: str, bundle_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [hdc, "uninstall", bundle_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _clean_reinstall(
    hdc: str, hap_path: str, bundle_name: str, require_uninstall_success: bool
) -> bool:
    uninstall_cmd = [hdc, "uninstall", bundle_name]
    print(f"  执行: {' '.join(uninstall_cmd)}")
    uninstall = _run_uninstall(hdc, bundle_name)
    if uninstall.returncode != 0:
        output = (uninstall.stdout or "") + (uninstall.stderr or "")
        if require_uninstall_success:
            print("[ERROR] 卸载原应用失败")
            _print_output_tail(output)
            return False
        print("  [WARN] 卸载原应用未成功，继续重试安装")
        _print_output_tail(output, lines=5)
    else:
        print("  [OK] 原应用已卸载")

    cmd = [hdc, "install", "-r", hap_path]
    print(f"  执行: {' '.join(cmd)}")
    retry = _run_install(hdc, hap_path)
    if retry.returncode != 0:
        print("[ERROR] 重试安装失败")
        _print_output_tail((retry.stdout or "") + (retry.stderr or ""))
        return False

    print("  等待设备确认安装结果 ...")
    if not _wait_for_bundle_on_device(hdc, bundle_name):
        print(f"[ERROR] 重试安装后仍未在设备上检测到包: {bundle_name}")
        _print_output_tail((retry.stdout or "") + (retry.stderr or ""))
        return False

    return True


def install(hdc: str, hap_path: str, bundle_name: str, dry_run: bool) -> bool:
    print(f"\n>>> [5/6] 安装 HAP 到设备")

    if not dry_run:
        r = subprocess.run(
            [hdc, "list", "targets"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        devs = [d.strip() for d in (r.stdout or "").split("\n") if d.strip()]
        if not devs:
            print("[ERROR] 未检测到已连接设备，请运行 hdc list targets 验证")
            return False
        print(f"  设备: {devs}")

    cmd = [hdc, "install", "-r", hap_path]
    print(f"  执行: {' '.join(cmd)}")
    if dry_run:
        print("  [DRY-RUN] 跳过安装")
        return True

    r = _run_install(hdc, hap_path)
    if r.returncode != 0:
        output = (r.stdout or "") + (r.stderr or "")
        can_retry = _is_existing_bundle_install_error(
            output, bundle_name
        ) and _bundle_exists_on_device(hdc, bundle_name)
        if not can_retry:
            print("[ERROR] 安装失败")
            _print_output_tail(output)
            return False

        print(f"  [WARN] 安装失败，设备上已存在同包名应用: {bundle_name}")
        _print_output_tail(output)
        if not _clean_reinstall(
            hdc, hap_path, bundle_name, require_uninstall_success=True
        ):
            return False
    else:
        print("  等待设备确认安装结果 ...")
        if not _wait_for_bundle_on_device(hdc, bundle_name):
            print(
                f"  [WARN] 安装指令返回成功，但设备上未检测到包: {bundle_name}"
            )
            print("  尝试清理后重新安装一次")
            if not _clean_reinstall(
                hdc, hap_path, bundle_name, require_uninstall_success=False
            ):
                return False
            if not _wait_for_bundle_on_device(hdc, bundle_name, attempts=3):
                print(f"[ERROR] 安装校验失败，设备上仍未检测到包: {bundle_name}")
                _print_output_tail((r.stdout or "") + (r.stderr or ""))
                return False

    print("  [OK] 安装成功")
    return True


def launch(hdc: str, bundle_name: str, dry_run: bool) -> bool:
    print(f"\n>>> [6/6] 启动应用")

    # 尝试从 bm dump 获取 ability 名
    ability = "EntryAbility"
    if not dry_run:
        try:
            r = subprocess.run(
                [hdc, "shell", "bm", "dump", "-n", bundle_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            m = re.search(r"ability:\s*([\w.]+)", r.stdout or "")
            if m:
                ability = m.group(1)
        except Exception:
            pass

    cmd = [hdc, "shell", "aa", "start", "-a", ability, "-b", bundle_name]
    print(f"  执行: {' '.join(cmd)}")
    if dry_run:
        print("  [DRY-RUN] 跳过启动")
        return True

    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    output = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0 or "success" in output.lower():
        print("  [OK] 应用启动指令已发送")
    else:
        print("[WARN] 启动可能有问题")
        for line in output.split("\n")[-5:]:
            if line.strip():
                print(f"  | {line.strip()}")
    return True


# =============================================================================
# 主入口
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HarmonyOS 配置/编译/部署/启动一体脚本"
    )
    parser.add_argument(
        "-y", "--yaml", default="config.yaml", help="配置文件路径，默认: ./config.yaml"
    )
    parser.add_argument("-p", "--project", required=True, help="鸿蒙工程根目录")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="跳过实际编译/安装/启动；配置写入仍会执行",
    )
    parser.add_argument("--skip-build", action="store_true", help="跳过编译")
    parser.add_argument("--skip-config", action="store_true", help="跳过配置写入")
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project)
    if not os.path.isdir(project_dir):
        sys.exit(f"[ERROR] 工程目录不存在: {project_dir}")

    # ---- 工具 ----
    tools = resolve_tools()
    print("=" * 60)
    print("工具路径:")
    for k, v in tools.items():
        print(f"  {k:8s} → {v}")

    # ---- 配置文件 ----
    cfg = load_config(args.yaml)
    bundle = cfg["bundleName"]
    signing = cfg["signingConfigs"]
    print(f"\n配置文件:")
    print(f"  path:           {args.yaml}")
    print(f"  bundleName:     {bundle}")
    print(f"  signingConfigs: {len(signing)} 组")

    # ---- 1-2. 配置 ----
    if not args.skip_config:
        configure_app(project_dir, bundle)
        configure_signing(project_dir, signing)
    else:
        print("\n[跳过] 配置文件写入 (--skip-config)")

    # ---- 3. 编译 ----
    if not args.skip_build:
        if not build(
            project_dir,
            tools["hvigorw"],
            tools.get("node"),
            tools.get("java"),
            tools.get("ohpm"),
            args.dry_run,
        ):
            sys.exit("\n[ABORT] 编译失败")
    else:
        print("\n[跳过] 编译 (--skip-build)")

    # ---- 4. 查找 HAP ----
    hap = find_hap(project_dir)
    if not hap:
        if args.dry_run:
            hap = "<path-to-signed.hap>"
            print("  [DRY-RUN] 未找到 HAP，使用占位路径")
        else:
            sys.exit("[ABORT] 未找到 HAP")

    # ---- 5. 安装 ----
    if not install(tools["hdc"], hap, bundle, args.dry_run):
        sys.exit("\n[ABORT] 安装失败")

    # ---- 6. 启动 ----
    launch(tools["hdc"], bundle, args.dry_run)

    print("\n" + "=" * 60)
    print("全部完成!")
    print("=" * 60)


if __name__ == "__main__":
    main()
