#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CI/CD 日志轮询脚本
==================

按一定时间间隔轮询 log-service (http://81.70.210.89:8080)，
针对每个 (仓库缩写, commit_id) 查找已上传的日志 zip 包，下载并解压。

调用方式（两种）：

  1) 命令行参数
     python3 log_poller.py --watch bgw=abc1234 --watch mc=def5678 \
         --out ./_cicd_logs --base-url http://81.70.220.89:8080

  2) 环境变量
     LOG_BASE_URL=http://81.70.220.89:8080
     LOG_OUT_DIR=./_cicd_logs
     LOG_POLL_INTERVAL=10
     LOG_TIMEOUT_SEC=1800
     LOG_WATCHES="bgw=abc1234,mc=def5678"

  --watch 形式： name=commit  （name 必须是 bgw/gids/mc，commit 是完整或前缀）
  --watch 也可写 name=  （只传 name，留空用于全量查询；不推荐混用）

退出码：
  0  全部 watch 的文件都已下载并解压完成
  2  超时（部分文件仍没出现）
  3  调用错误（参数/网络）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# log-service 端点
DEFAULT_BASE_URL = "http://81.70.210.89:8080"
HEALTH_URL_SUFFIX = "/"
QUERY_URL_SUFFIX = "/query"
DOWNLOAD_URL_SUFFIX = "/download"

# 文件名正则（与 log-service/API.md 一致）
FILENAME_RE = re.compile(r"^[a-zA-Z0-9\-]+_[a-zA-Z0-9\-]+_\d{14}\.zip$")

# 项目允许的 name 集合
ALLOWED_NAMES = {"bgw", "gids", "mc"}

# 默认参数
DEFAULT_POLL_INTERVAL = 10        # 秒
DEFAULT_TIMEOUT_SEC = 30 * 60    # 30 分钟

# skill 默认配置文件路径（脚本同目录上一级），可用 --config 覆盖
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config.yaml"
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Watch:
    name: str               # 仓库缩写 bgw/gids/mc
    commit: str             # commit_id（完整或前缀）
    files: List[str] = field(default_factory=list)   # 已查询到并下载过的文件名
    done: bool = False      # 是否已经下载到第一个（或指定）文件


@dataclass
class State:
    base_url: str
    out_dir: Path
    watches: Dict[str, Watch]   # key = name
    raw_dir: Path = field(init=False)
    extracted_dir: Path = field(init=False)
    state_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.raw_dir = self.out_dir / "raw"
        self.extracted_dir = self.out_dir / "extracted"
        self.state_file = self.out_dir / "state.json"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.extracted_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _coerce(value: str):
    """YAML 字面量到 Python 值。仅支持本 skill 需要的标量/列表/字典。"""
    v = value.strip()
    if v == "" or v.lower() in {"null", "~"}:
        return None
    if (v.startswith('"') and v.endswith('"')) or (
        v.startswith("'") and v.endswith("'")
    ):
        return v[1:-1]
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def load_config(path: Optional[Path]) -> dict:
    """
    读取 YAML 配置文件。优先用 PyYAML（已装），回退到内置极简解析（仅
    支持本 skill 需要的标量/嵌套字典/列表，且 key 后必须为 - item 而非
    嵌套字典）。文件不存在返回 {}。
    """
    if path is None or not Path(path).exists():
        return {}
    text = Path(path).read_text("utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"config {path} 顶层必须是字典，实际是 {type(data).__name__}"
            )
        return data
    except ImportError:
        pass
    # 回退路径
    return _mini_yaml_load(text, str(path))


def _mini_yaml_load(text: str, path: str) -> dict:
    """PyYAML 不可用时的兜底。能力：嵌套 dict、标量、列表（仅 - item 形式）。
    块嵌套字典/列表、复杂引用、类型标签等一概不支持。"""
    root: Dict[str, object] = {}
    stack: List[Tuple[int, object, str]] = [(-1, root, "dict")]
    last_list_under: Dict[int, list] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        m_list = re.match(r"^(\s*)- (.*)$", line)
        if m_list:
            indent = len(m_list.group(1))
            item = _coerce(m_list.group(2).strip())
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if not stack or stack[-1][2] != "dict":
                raise ValueError(
                    f"config {path}:{lineno} 列表找不到父字典: {raw!r}"
                )
            target = last_list_under.get(id(stack[-1][1]))
            if target is None:
                raise ValueError(
                    f"config {path}:{lineno} 列表项前无对应 key: {raw!r}"
                )
            target.append(item)
            continue
        m = re.match(r"^(\s*)([^:]+?):\s*(.*)$", line)
        if not m:
            raise ValueError(f"config {path}:{lineno} 无法解析: {raw!r}")
        indent = len(m.group(1))
        key = m.group(2).strip()
        rest = m.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack or stack[-1][2] != "dict":
            raise ValueError(
                f"config {path}:{lineno} 父节点不是字典: {raw!r}"
            )
        parent_dict = stack[-1][1]
        if rest == "":
            new_dict: Dict[str, object] = {}
            parent_dict[key] = new_dict
            stack.append((indent, new_dict, "dict"))
        else:
            parent_dict[key] = _coerce(rest)
    return root


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[poller {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_watch(spec: str) -> Tuple[str, str]:
    """解析 name=commit 字符串。"""
    if "=" not in spec:
        raise ValueError(f"--watch 格式错误：{spec!r}，应为 name=commit")
    name, commit = spec.split("=", 1)
    name = name.strip()
    commit = commit.strip()
    if name not in ALLOWED_NAMES:
        raise ValueError(f"--watch 名称非法 {name!r}，只允许 {sorted(ALLOWED_NAMES)}")
    if not name or not commit:
        raise ValueError(f"--watch 名称或 commit 为空：{spec!r}")
    return name, commit


def parse_watches_from_env(env_value: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for item in env_value.split(","):
        item = item.strip()
        if not item:
            continue
        out.append(parse_watch(item))
    return out


def health_check(session: requests.Session, base_url: str) -> bool:
    try:
        r = session.get(base_url.rstrip("/") + HEALTH_URL_SUFFIX, timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


def query_files(
    session: requests.Session,
    base_url: str,
    name: str,
    commit_prefix: str,
) -> List[dict]:
    """调 /query?name=...&commit=...。"""
    url = base_url.rstrip("/") + QUERY_URL_SUFFIX
    params = {"name": name, "commit": commit_prefix}
    r = session.get(url, params=params, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"/query 返回错误：{body}")
    files = (body.get("data") or {}).get("files") or []
    # 二次过滤：API 对 commit 是前缀匹配；为了避免模糊匹配到别的 commit，
    # 这里严格校验文件名前缀 == 我们的 commit_prefix（不区分大小写）。
    files = [
        f for f in files
        if f["name"].split("_")[1].lower().startswith(commit_prefix.lower())
    ]
    return files


def download_zip(
    session: requests.Session,
    base_url: str,
    filename: str,
    dst_zip: Path,
) -> None:
    url = base_url.rstrip("/") + DOWNLOAD_URL_SUFFIX
    params = {"filename": filename}
    r = session.get(url, params=params, timeout=60, stream=True)
    r.raise_for_status()
    if r.status_code != 200:
        raise RuntimeError(f"/download 失败 {r.status_code} {r.text[:200]}")
    with open(dst_zip, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)


def extract_zip(zip_path: Path, out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted: List[Path] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            # 防御 zip slip
            target = (out_dir / member).resolve()
            if not str(target).startswith(str(out_dir.resolve())):
                raise RuntimeError(f"非法 zip 路径：{member}")
            zf.extract(member, out_dir)
            extracted.append(target)
    return extracted


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_state(args: argparse.Namespace, cfg: dict) -> State:
    log_svc = cfg.get("log_service", {}) if isinstance(cfg, dict) else {}
    paths = cfg.get("paths", {}) if isinstance(cfg, dict) else {}

    if args.watch:
        watches_list = [parse_watch(w) for w in args.watch]
    else:
        env = os.environ.get("LOG_WATCHES", "").strip()
        cfg_watches = ""
        if isinstance(paths.get("watches"), list):
            cfg_watches = ",".join(
                str(x) for x in paths["watches"] if str(x).strip()
            )
        merged = env or cfg_watches
        if not merged:
            raise SystemExit(
                "未指定 watch：传 --watch name=commit，或设环境变量 LOG_WATCHES，"
                "或在 config.yaml 的 paths.watches 列表里写"
            )
        watches_list = parse_watches_from_env(merged)

    base_url = (
        args.base_url
        or os.environ.get("LOG_BASE_URL")
        or log_svc.get("url", DEFAULT_BASE_URL)
    )
    out_dir = Path(
        args.out_dir
        or os.environ.get("LOG_OUT_DIR")
        or paths.get("out_dir", "./_cicd_logs")
    ).resolve()

    watches = {
        name: Watch(name=name, commit=commit) for name, commit in watches_list
    }
    return State(base_url=base_url, out_dir=out_dir, watches=watches)


def load_existing_state(state: State) -> None:
    """若 state.json 存在，把已经处理过的文件读回来，避免重复下载。"""
    if not state.state_file.exists():
        return
    try:
        data = json.loads(state.state_file.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"读取 state.json 失败：{e}，将重新开始")
        return
    for name, w in data.get("watches", {}).items():
        if name in state.watches:
            state.watches[name].files = list(w.get("files", []))
            state.watches[name].done = bool(w.get("done", False))


def save_state(state: State) -> None:
    payload = {
        "base_url": state.base_url,
        "out_dir": str(state.out_dir),
        "watches": {
            name: {"commit": w.commit, "files": w.files, "done": w.done}
            for name, w in state.watches.items()
        },
    }
    state.state_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
    )


def process_one_file(
    session: requests.Session,
    state: State,
    watch: Watch,
    file_meta: dict,
) -> bool:
    """下载 + 解压一个文件。返回是否成功。"""
    name = file_meta["name"]
    if not FILENAME_RE.match(name):
        log(f"  ! 文件名不符合规范，跳过：{name}")
        return False

    raw_dir = state.raw_dir / watch.name
    raw_dir.mkdir(parents=True, exist_ok=True)
    zip_path = raw_dir / name

    # 抽取 commit_id 段作为子目录
    commit_seg = name.split("_")[1]
    extract_dir = state.extracted_dir / watch.name / commit_seg
    extract_dir.mkdir(parents=True, exist_ok=True)

    # 如果这个 zip 已经下载过且已解压有文件，跳过
    if zip_path.exists() and any(extract_dir.iterdir()):
        log(f"  · 已存在：{name}")
        watch.files.append(name)
        watch.done = True
        return True

    log(f"  ↓ 下载 {name} （{file_meta.get('size', '?')} bytes）")
    try:
        download_zip(session, state.base_url, name, zip_path)
    except Exception as e:  # noqa: BLE001
        log(f"  ! 下载失败：{e}")
        if zip_path.exists():
            zip_path.unlink()
        return False

    log(f"  📦 解压到 {extract_dir}")
    try:
        files = extract_zip(zip_path, extract_dir)
    except Exception as e:  # noqa: BLE001
        log(f"  ! 解压失败：{e}")
        return False

    for f in files[:20]:
        log(f"     - {f.relative_to(state.out_dir)}")
    if len(files) > 20:
        log(f"     ... 共 {len(files)} 个文件")

    watch.files.append(name)
    watch.done = True
    return True


def run(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    cfg = load_config(cfg_path)
    log_svc = cfg.get("log_service", {}) if isinstance(cfg, dict) else {}

    interval = int(
        args.interval
        or os.environ.get("LOG_POLL_INTERVAL")
        or log_svc.get("poll_interval_sec", DEFAULT_POLL_INTERVAL)
    )
    timeout = int(
        args.timeout
        or os.environ.get("LOG_TIMEOUT_SEC")
        or log_svc.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
    )

    state = build_state(args, cfg)
    load_existing_state(state)
    if cfg_path.exists():
        log(f"已加载配置: {cfg_path}")

    log(f"日志服务: {state.base_url}")
    log(f"输出目录: {state.out_dir}")
    log(f"轮询间隔: {interval}s，超时: {timeout}s")
    for name, w in state.watches.items():
        log(f"  watch: {name} = {w.commit}{'（已完成）' if w.done else ''}")

    session = requests.Session()

    if not health_check(session, state.base_url):
        log(f"!! 警告：日志服务 {state.base_url} 健康检查失败，继续轮询…")

    deadline = time.time() + timeout
    round_idx = 0
    while True:
        round_idx += 1
        log(f"--- 第 {round_idx} 轮查询 ---")
        all_done = True
        for name, w in state.watches.items():
            if w.done:
                continue
            all_done = False
            try:
                files = query_files(
                    session, state.base_url, name, w.commit
                )
            except Exception as e:  # noqa: BLE001
                log(f"  ! {name} 查询失败：{e}")
                continue
            log(f"  · {name} (commit={w.commit}) 命中 {len(files)} 个文件")
            if not files:
                continue
            # 取第一个匹配（最新/唯一）
            for meta in files:
                ok = process_one_file(session, state, w, meta)
                if ok:
                    break
        save_state(state)

        if all(w.done for w in state.watches.values()):
            log("✓ 全部 watch 已完成")
            print_summary(state)
            return 0

        if time.time() > deadline:
            missing = [w for w in state.watches.values() if not w.done]
            log(f"!! 超时，仍未完成的 watch: {[w.name for w in missing]}")
            print_summary(state)
            return 2

        time.sleep(interval)


def print_summary(state: State) -> None:
    log("== 产物清单 ==")
    for name, w in state.watches.items():
        status = "✓" if w.done else "✗"
        log(f"  {status} {name} (commit={w.commit})")
        for f in w.files:
            log(f"      - {f}")
    log(f"原始 zip:   {state.raw_dir}")
    log(f"解压目录:   {state.extracted_dir}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument(
        "--watch", action="append", default=[],
        help="name=commit 形式的 watch，可重复传；name 仅限 bgw/gids/mc",
    )
    p.add_argument(
        "--config", default=None,
        help=f"配置文件路径，默认 {DEFAULT_CONFIG_PATH}",
    )
    p.add_argument(
        "--base-url", default=None,
        help=f"log-service 基础 URL，默认 {DEFAULT_BASE_URL}",
    )
    p.add_argument("--out", dest="out_dir", default=None, help="产物输出目录")
    p.add_argument(
        "--interval", type=int, default=None,
        help=f"轮询间隔秒数，默认 {DEFAULT_POLL_INTERVAL}",
    )
    p.add_argument(
        "--timeout", type=int, default=None,
        help=f"总超时秒数，默认 {DEFAULT_TIMEOUT_SEC}",
    )
    p.add_argument(
        "--once", action="store_true",
        help="只跑一轮（不循环），方便调试",
    )
    args = p.parse_args()

    try:
        return run(args)
    except KeyboardInterrupt:
        log("用户中断")
        return 130
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        log(f"致命错误：{e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
