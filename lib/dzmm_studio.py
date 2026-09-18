#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DZMM Game Studio bridge: sync local project to online workbench container and publish.

Auth (.env, gitignored):
  email=...
  password=...
  cookie=sb-rls-auth-token=base64-...   # optional; auto-refreshed / rewritten

Access token ~1h；脚本会在快过期时用 cookie 调 GET /api/auth/token 自动续期。
refresh 失效或没有 cookie 时，可用：
  - email+password → POST /api/auth/sign-in
  - 登录码图片 → POST /api/auth/sign-in-code/scan
  - Telegram 扫码 → /api/auth/tg-sign-in-code + 轮询
  - 粘贴浏览器 Cookie（assemble_auth_cookie）

Target workbench: https://www.dzmm.ai/studio/game-creation/workbench?character_id=<id>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client as http_client
import json
import mimetypes
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = KIT_ROOT / "config.json"
ENV_PATH = KIT_ROOT / ".env"
# 海外主站；国内易被拦时请换线路（见 ADDR_PAGE / BUILTIN_ORIGINS）
DEFAULT_ORIGIN = "https://www.dzmm.ai"
ADDR_PAGE = "https://dzmm-home.github.io/dzmm-addr/"
# 与官方线路页国内池一致（测速用 /api/heartbeat）；海外主站放最后兜底
BUILTIN_ORIGINS = [
    "https://www.aifukk.com",
    "https://www.fuckaibot.com",
    "https://www.thottai.com",
    "https://www.aicbnv.com",
    "https://www.aikda.com",
    "https://www.ainvmei.com",
    "https://www.girlloveai.com",
    "https://www.meimoaidao.com",
    "https://www.loreveil.xyz",
    "https://www.museloom.xyz",
    "https://www.echolore.xyz",
    DEFAULT_ORIGIN,
]
ORIGIN = DEFAULT_ORIGIN
AUTH_REFRESH_SKEW = 180  # refresh when < 3 minutes left
DEFAULT_CARD_ID = 0
HTTP_RETRY_ATTEMPTS = 5
HTTP_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_TRANSIENT_NET_MARKERS = (
    "unexpected_eof",
    "eof occurred in violation of protocol",
    "ssleoferror",
    "ssl: unexpected_eof",
    "connection reset",
    "connection aborted",
    "connection refused",
    "broken pipe",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "temporary failure",
    "winerror 10054",
    "winerror 10053",
    "winerror 10060",
    "remote end closed",
    "remotely closed",
    "incomplete read",
    "bad handshake",
    "the handshake operation timed out",
)


def normalize_origin(raw) -> str:
    """接受完整 URL 或裸域名，统一成 https://www.host。"""
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_ORIGIN
    if "://" not in text:
        text = "https://" + text
    try:
        u = urllib.parse.urlsplit(text)
    except Exception:
        return DEFAULT_ORIGIN
    host = (u.hostname or "").lower().strip(".")
    if not host:
        return DEFAULT_ORIGIN
    if not host.startswith("www.") and host.count(".") == 1:
        host = "www." + host
    scheme = "https"
    return f"{scheme}://{host}"


def list_origin_choices() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in BUILTIN_ORIGINS:
        o = normalize_origin(item)
        if o not in seen:
            seen.add(o)
            out.append(o)
    cur = get_origin()
    if cur not in seen:
        out.insert(0, cur)
    return out


def get_origin() -> str:
    """当前 API / 鉴权线路。优先 config.json origin，其次 .env origin。"""
    global ORIGIN
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg = data
        except Exception:
            cfg = {}
    env = _read_env_map_raw()
    raw = (cfg.get("origin") or env.get("origin") or env.get("ORIGIN") or "").strip()
    ORIGIN = normalize_origin(raw) if raw else DEFAULT_ORIGIN
    return ORIGIN


def set_origin(raw, *, clear_cookie: bool = True) -> str:
    """切换线路并写入 config；换域后旧 cookie 通常失效，默认清除。"""
    global ORIGIN
    origin = normalize_origin(raw)
    save_config({"origin": origin})
    ORIGIN = origin
    if clear_cookie:
        try:
            _save_env(clear_cookie=True)
        except Exception:
            pass
    return origin


def probe_origins(timeout: float = 6.0) -> list[dict]:
    """并行测速各线路（对齐官方页：GET /api/heartbeat）。"""
    import concurrent.futures

    origins = list_origin_choices()

    def _one(origin: str) -> dict:
        url = origin.rstrip("/") + f"/api/heartbeat?_={int(time.time() * 1000)}"
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 DZMM-Studio-Bridge",
                    "Accept": "application/json",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                ms = int((time.perf_counter() - t0) * 1000)
                ok = 200 <= int(resp.status) < 500
                return {
                    "origin": origin,
                    "ok": ok,
                    "ms": ms,
                    "status": int(resp.status),
                    "bytes": len(raw or b""),
                }
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            return {
                "origin": origin,
                "ok": False,
                "ms": ms,
                "status": 0,
                "error": str(e),
            }

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(origins) or 1)) as pool:
        futs = [pool.submit(_one, o) for o in origins]
        for fut in concurrent.futures.as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: (0 if r.get("ok") else 1, int(r.get("ms") or 10**9)))
    return results


def pick_fastest_origin(timeout: float = 6.0) -> dict:
    """测速并自动切到最快可用线路。"""
    results = probe_origins(timeout=timeout)
    best = next((r for r in results if r.get("ok")), None)
    if not best:
        return {
            "ok": False,
            "error": "所有线路均不可用，请稍后重试或打开官方线路页",
            "addrPage": ADDR_PAGE,
            "results": results,
            "origin": get_origin(),
        }
    origin = set_origin(best["origin"], clear_cookie=True)
    return {
        "ok": True,
        "origin": origin,
        "ms": best.get("ms"),
        "addrPage": ADDR_PAGE,
        "results": results,
        "message": f"已切换到 {origin}（{best.get('ms')}ms）。请重新登录（换域后旧 cookie 无效）。",
    }


_CONFIG_LOAD_ERROR = ""


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically; on Windows tolerate locked destinations (editor/AV)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    last_err: Exception | None = None
    for attempt in range(8):
        try:
            # Clear read-only if someone set it; ignore failures.
            try:
                if path.exists() and not os.access(path, os.W_OK):
                    path.chmod(path.stat().st_mode | 0o200)
            except Exception:
                pass
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.05 * (attempt + 1))
        except OSError as e:
            # WinError 5 / sharing violation often surfaces as PermissionError or OSError
            last_err = e
            if getattr(e, "winerror", None) not in (5, 32) and e.errno not in (13, 11):
                break
            time.sleep(0.05 * (attempt + 1))
    # Fallback: overwrite in place (still better than crashing the console request)
    try:
        path.write_text(text, encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise last_err or e


def load_config() -> dict:
    global _CONFIG_LOAD_ERROR
    cfg: dict = {
        "character_id": 0,
        "project_path": "",
        "preview_port": 8791,
        "origin": DEFAULT_ORIGIN,
    }
    _CONFIG_LOAD_ERROR = ""
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
            else:
                _CONFIG_LOAD_ERROR = "config.json 不是对象"
        except Exception as e:
            _CONFIG_LOAD_ERROR = f"config.json 损坏: {e}"
    env = _read_env_map_raw()
    if env.get("character_id") and not cfg.get("character_id"):
        try:
            cfg["character_id"] = int(env["character_id"])
        except Exception:
            pass
    if env.get("project_path") and not cfg.get("project_path"):
        cfg["project_path"] = env["project_path"]
    if env.get("origin") and not cfg.get("origin"):
        cfg["origin"] = env["origin"]
    if cfg.get("origin"):
        cfg["origin"] = normalize_origin(cfg["origin"])
    get_origin()
    return cfg


def _looks_like_player_package(dir_path: Path) -> bool:
    """目录本身是否像玩家包（根上就有 index.html）。"""
    if not (dir_path / "index.html").is_file():
        return False
    markers = (
        "game.js",
        "app.js",
        "main.js",
        "config.js",
        "style.css",
        "assets",
        "libs",
        "functions",
        "template.json",
    )
    return any((dir_path / name).exists() for name in markers)


def resolve_game_project(raw) -> dict:
    """智能识别游戏项目路径，不依赖写死目录名之外的布局约定。

    返回字段：
      ok, root, publish_dir, index, layout(nested|flat), error, hint, failedPaths
    """
    empty = {
        "ok": False,
        "root": Path(),
        "publish_dir": Path(),
        "index": Path(),
        "layout": "",
        "error": "未设置游戏项目路径",
        "hint": "",
        "failedPaths": [],
    }
    text = str(raw or "").strip()
    if not text:
        return dict(empty)

    path = Path(text).expanduser()
    try:
        path = path.resolve()
    except OSError:
        path = Path(text).expanduser()

    def _ok(root: Path, publish_dir: Path, layout: str, hint: str = "") -> dict:
        return {
            "ok": True,
            "root": root,
            "publish_dir": publish_dir,
            "index": publish_dir / "index.html",
            "layout": layout,
            "error": "",
            "hint": hint,
            "failedPaths": [],
        }

    if not path.exists():
        return {
            **empty,
            "root": path,
            "publish_dir": path / "publish",
            "index": path / "publish" / "index.html",
            "error": f"路径不存在：{path}",
        }

    # 标准：根/publish/index.html
    if path.is_dir() and (path / "publish" / "index.html").is_file():
        return _ok(path, path / "publish", "nested")

    # 误填到 …/publish（有或没有 index 都上溯到项目根再识别）
    if path.is_dir() and path.name.lower() == "publish":
        if (path / "index.html").is_file():
            return _ok(path.parent, path, "nested", "已自动上溯到项目根（你填的是 publish/）")
        parent_resolved = resolve_game_project(path.parent)
        if parent_resolved.get("hint") or parent_resolved.get("error"):
            # 保留父级识别结果；补充说明填的是 publish 子目录
            if parent_resolved.get("ok"):
                parent_resolved["hint"] = (
                    (parent_resolved.get("hint") + " · ") if parent_resolved.get("hint") else ""
                ) + "已自动上溯到项目根（你填的是 publish/）"
            return parent_resolved

    # 扁平玩家包：目录根就是 index.html
    if path.is_dir() and _looks_like_player_package(path):
        parent = path.parent
        if (parent / "publish" / "index.html").is_file():
            return _ok(parent, parent / "publish", "nested", "已识别父级标准项目布局")
        return _ok(path, path, "flat", "已识别为扁平玩家包（根目录即 publish 内容）")

    # 多填了一层：子目录里才有 publish/index.html
    if path.is_dir():
        try:
            for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if (child / "publish" / "index.html").is_file():
                    return _ok(
                        child,
                        child / "publish",
                        "nested",
                        f"已自动识别子目录：{child.name}",
                    )
                if _looks_like_player_package(child):
                    return _ok(
                        child,
                        child,
                        "flat",
                        f"已自动识别子目录玩家包：{child.name}",
                    )
        except OSError:
            pass

    # publish/ 在，但 index 缺失（常见：拉取失败）
    failed_paths: list[str] = []
    if path.is_dir():
        meta_path = path / "_pull_meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    failed_paths = [str(x) for x in (meta.get("failed_paths") or [])]
            except Exception:
                failed_paths = []

    pub = path / "publish" if path.is_dir() else path
    if path.is_dir() and pub.is_dir() and not (pub / "index.html").is_file():
        idx_failed = any(
            "publish/index.html" in f.replace("\\", "/")
            or f.replace("\\", "/").endswith("/index.html")
            for f in failed_paths
        )
        if failed_paths or idx_failed:
            err = (
                f"项目目录已识别：{path}\n"
                f"但 publish/index.html 还没下下来"
                f"（拉取失败 {len(failed_paths)} 个，含 index.html）。\n"
                f"请到侧栏「拉取容器」点「重拉失败文件」，不要改路径。"
            )
        else:
            err = (
                f"项目目录已识别：{path}\n"
                f"已有 publish/，但缺少 index.html。\n"
                f"请先拉取容器，或确认云端项目里存在 publish/index.html。"
            )
        return {
            **empty,
            "root": path,
            "publish_dir": pub,
            "index": pub / "index.html",
            "layout": "nested",
            "error": err,
            "failedPaths": failed_paths,
        }

    return {
        **empty,
        "root": path,
        "publish_dir": path / "publish" if path.is_dir() else path,
        "index": (path / "publish" / "index.html") if path.is_dir() else path,
        "error": (
            f"在 {path} 下没找到可预览的 index.html。\n"
            f"可填：① 含 publish/index.html 的项目根；② 根上直接有 index.html 的玩家包目录。"
        ),
        "failedPaths": failed_paths,
    }


def normalize_project_root(raw) -> Path:
    """归一化为游戏项目根（供配置保存）。优先用智能识别结果。"""
    resolved = resolve_game_project(raw)
    if resolved.get("ok") and resolved.get("root"):
        return Path(resolved["root"])
    path = Path(str(raw or "")).expanduser()
    try:
        return path.resolve()
    except OSError:
        return path


def save_config(updates: dict) -> dict:
    global ORIGIN, _CONFIG_LOAD_ERROR
    cfg = load_config()
    if _CONFIG_LOAD_ERROR and CONFIG_PATH.exists():
        # 损坏时拒绝覆盖，避免把 character_id / project_path 冲成默认值
        raise RuntimeError(f"{_CONFIG_LOAD_ERROR}；请先修复或删除 config.json 后再保存")
    cfg.update({k: v for k, v in updates.items() if v is not None})
    if "character_id" in cfg:
        try:
            cfg["character_id"] = int(cfg["character_id"] or 0)
        except Exception:
            cfg["character_id"] = 0
    if "preview_port" in cfg:
        try:
            cfg["preview_port"] = int(cfg["preview_port"] or 8791)
        except Exception:
            cfg["preview_port"] = 8791
    if cfg.get("project_path"):
        cfg["project_path"] = str(normalize_project_root(cfg["project_path"]))
    if cfg.get("origin"):
        cfg["origin"] = normalize_origin(cfg["origin"])
        ORIGIN = cfg["origin"]
    # Keep recent project switch list in sync with current selection
    if "character_id" in updates or "project_path" in updates:
        cfg["project_history"] = _push_project_history(
            cfg,
            character_id=int(cfg.get("character_id") or 0),
            project_path=str(cfg.get("project_path") or ""),
            label=str(updates.get("project_label") or "").strip() or None,
        )
    cfg.pop("project_label", None)
    _atomic_write_text(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    _CONFIG_LOAD_ERROR = ""
    return cfg


PROJECT_HISTORY_MAX = 12


def _project_history_label(project_path: str, label: str | None = None) -> str:
    if label and str(label).strip():
        return str(label).strip()[:80]
    raw = (project_path or "").strip()
    if not raw:
        return ""
    try:
        name = Path(raw).name
    except Exception:
        name = raw
    return (name or raw)[:80]


def normalize_project_history(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            cid = int(item.get("character_id") or 0)
        except Exception:
            cid = 0
        path = str(item.get("project_path") or "").strip()
        if cid <= 0 or not path:
            continue
        try:
            path = str(normalize_project_root(path))
        except Exception:
            pass
        key = f"{cid}|{path.replace('/', '\\').lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "character_id": cid,
                "project_path": path,
                "label": _project_history_label(path, item.get("label")),
                "last_used": str(item.get("last_used") or ""),
            }
        )
        if len(out) >= PROJECT_HISTORY_MAX:
            break
    return out


def list_project_history(cfg: dict | None = None) -> list[dict]:
    cfg = cfg if isinstance(cfg, dict) else load_config()
    hist = normalize_project_history(cfg.get("project_history"))
    # Ensure current project appears even if history was empty
    try:
        cid = int(cfg.get("character_id") or 0)
    except Exception:
        cid = 0
    path = str(cfg.get("project_path") or "").strip()
    if cid > 0 and path:
        try:
            path = str(normalize_project_root(path))
        except Exception:
            pass
        key = f"{cid}|{path.replace('/', '\\').lower()}"
        if not any(f"{h['character_id']}|{h['project_path'].replace('/', '\\').lower()}" == key for h in hist):
            hist.insert(
                0,
                {
                    "character_id": cid,
                    "project_path": path,
                    "label": _project_history_label(path),
                    "last_used": "",
                },
            )
            hist = hist[:PROJECT_HISTORY_MAX]
    return hist


def _push_project_history(
    cfg: dict,
    *,
    character_id: int,
    project_path: str,
    label: str | None = None,
) -> list[dict]:
    from datetime import datetime, timezone

    cid = int(character_id or 0)
    path = str(project_path or "").strip()
    if cid <= 0 or not path:
        return normalize_project_history(cfg.get("project_history"))
    try:
        path = str(normalize_project_root(path))
    except Exception:
        pass
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "character_id": cid,
        "project_path": path,
        "label": _project_history_label(path, label),
        "last_used": now,
    }
    key = f"{cid}|{path.replace('/', '\\').lower()}"
    rest = []
    for h in normalize_project_history(cfg.get("project_history")):
        hk = f"{h['character_id']}|{h['project_path'].replace('/', '\\').lower()}"
        if hk == key:
            if h.get("label") and not label:
                entry["label"] = h["label"]
            continue
        rest.append(h)
    return [entry] + rest[: PROJECT_HISTORY_MAX - 1]


def remove_project_history_entry(*, character_id: int, project_path: str) -> list[dict]:
    cfg = load_config()
    cid = int(character_id or 0)
    path = str(project_path or "").strip()
    try:
        path = str(normalize_project_root(path)) if path else ""
    except Exception:
        pass
    key = f"{cid}|{path.replace('/', '\\').lower()}"
    hist = [
        h
        for h in normalize_project_history(cfg.get("project_history"))
        if f"{h['character_id']}|{h['project_path'].replace('/', '\\').lower()}" != key
    ]
    cfg["project_history"] = hist
    _atomic_write_text(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return hist


def project_root() -> Path:
    cfg = load_config()
    raw = (cfg.get("project_path") or "").strip()
    if raw:
        return normalize_project_root(raw)
    return KIT_ROOT


# Back-compat aliases used throughout this module
ROOT = KIT_ROOT  # overwritten by refresh_root()


def refresh_root() -> Path:
    global ROOT, DEFAULT_CARD_ID
    ROOT = project_root()
    cfg = load_config()
    DEFAULT_CARD_ID = int(cfg.get("character_id") or 0)
    return ROOT


def _read_env_map_raw() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    cookie_chunks: list[str] = []
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        lk = key.lower()
        if lk in ("email", "dzmm_email", "user", "username"):
            out["email"] = val
        elif lk in ("password", "dzmm_password", "pass"):
            out["password"] = val
        elif lk == "cookie":
            cookie_chunks.append(val)
        else:
            out[key] = val
    if cookie_chunks:
        out["cookie"] = cookie_chunks[-1]
    elif not out and ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding="utf-8").strip()
        if text:
            out["cookie"] = text[len("cookie=") :] if text.lower().startswith("cookie=") else text
    return out

# Paths synced into container (relative to repo root)
SYNC_GLOBS = [
    "publish/**/*",
    "functions/**/*",
    "AGENTS.md",
    "README.md",
    "CUSTOMIZATION.md",
    "template.json",
    "index.html",
    "favicon.svg",
    "icons.svg",
    "使用说明.txt",
    "assets/**/*",
    # 扁平玩家包（根目录即入口）常见结构
    "src/**/*",
    "data/**/*",
]

SYNC_SKIP_REL = {
    "_pull_meta.json",
    "_sync_meta.json",
    "本地开发说明.md",
}

SKIP_PARTS = {".git", "node_modules", "__pycache__", "tools", ".cursor"}
SYNC_META_NAME = "_sync_meta.json"


def _read_env_map() -> dict[str, str]:
    return _read_env_map_raw()


COOKIE_NAME = "sb-rls-auth-token"


def _finalize_cookie(value: str) -> str:
    """把 session value 规范成 `sb-rls-auth-token=base64-...`。"""
    value = (value or "").strip()
    if not value:
        return ""
    if value.lower().startswith("cookie="):
        value = value[len("cookie=") :].strip()
    while value.startswith(f"{COOKIE_NAME}={COOKIE_NAME}="):
        value = value[len(f"{COOKIE_NAME}=") :]
    if value.startswith(f"{COOKIE_NAME}="):
        value = value.split("=", 1)[1]
    if value.startswith("eyJ") and not value.startswith("base64-"):
        value = "base64-" + value
    if not value.startswith("base64-") and not value.startswith("eyJ"):
        # 已是完整 JSON base64 或其它；仍挂上名字
        pass
    if value.startswith("base64-") or value.startswith("eyJ"):
        return f"{COOKIE_NAME}={value if value.startswith('base64-') else 'base64-' + value}"
    return f"{COOKIE_NAME}={value}"


def assemble_auth_cookie(raw: str) -> str:
    """把浏览器复制的 Cookie 头 / 多行 / 分段 (.0/.1) 组装成完整单条 cookie。

    支持：
    - sb-rls-auth-token=base64-...
    - sb-rls-auth-token.0=... + .1=...（Supabase 超长 session 分片）
    - 仅 value（base64-... / eyJ...）
    - 整段 Cookie: a=1; sb-rls-auth-token=...; 其它=...
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    parts: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if ";" in line and "=" in line:
            parts.extend(p.strip() for p in line.split(";") if p.strip())
        else:
            parts.append(line)

    single = ""
    chunks: dict[int, str] = {}
    bare_values: list[str] = []
    for part in parts:
        if "=" not in part:
            bare_values.append(part.strip().strip('"'))
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"')
        if name == COOKIE_NAME:
            single = value
            continue
        if name.startswith(COOKIE_NAME + "."):
            suffix = name[len(COOKIE_NAME) + 1 :]
            if suffix.isdigit():
                chunks[int(suffix)] = value
                continue
        # 其它 sb-*-auth-token（容错）
        if re.fullmatch(r"sb-[A-Za-z0-9_-]+-auth-token", name):
            if not single:
                single = value
            continue
        m = re.fullmatch(r"(sb-[A-Za-z0-9_-]+-auth-token)\.(\d+)", name)
        if m:
            chunks[int(m.group(2))] = value

    if chunks:
        value = "".join(chunks[i] for i in sorted(chunks))
    elif single:
        value = single
    elif bare_values:
        value = "".join(bare_values)
    else:
        return ""
    return _finalize_cookie(value)


def _normalize_cookie(cookie: str) -> str:
    cookie = (cookie or "").strip()
    if not cookie:
        return ""
    # 整段头 / 多行 / 分片 → 先组装
    if (
        cookie.lower().startswith("cookie:")
        or ("\n" in cookie)
        or (";" in cookie)
        or (f"{COOKIE_NAME}." in cookie)
        or re.search(r"sb-[A-Za-z0-9_-]+-auth-token\.\d+", cookie)
    ):
        assembled = assemble_auth_cookie(cookie)
        if assembled:
            return assembled
    return _finalize_cookie(cookie)


def _session_from_cookie(cookie: str) -> dict:
    cookie = _normalize_cookie(cookie)
    if not cookie:
        raise ValueError("empty cookie")
    b64 = cookie.split("=", 1)[1]
    payload = b64[len("base64-") :] if b64.startswith("base64-") else b64
    payload += "=" * ((-len(payload)) % 4)
    # urlsafe 兼容
    try:
        return json.loads(base64.b64decode(payload))
    except Exception:
        return json.loads(base64.urlsafe_b64decode(payload))


def _cookie_from_jar(jar: CookieJar) -> str | None:
    single = ""
    chunks: dict[int, str] = {}
    for c in jar:
        if not c.name or not c.value:
            continue
        if c.name == COOKIE_NAME:
            single = c.value
            continue
        if c.name.startswith(COOKIE_NAME + "."):
            suffix = c.name[len(COOKIE_NAME) + 1 :]
            if suffix.isdigit():
                chunks[int(suffix)] = c.value
    if chunks:
        return _normalize_cookie("".join(chunks[i] for i in sorted(chunks)))
    if single:
        return _normalize_cookie(f"{COOKIE_NAME}={single}")
    return None


def current_auth_cookie() -> str:
    """返回本机已保存的完整 cookie（已归一化）。"""
    env = _read_env_map()
    raw = (env.get("cookie") or "").strip()
    return _normalize_cookie(raw) if raw else ""


def _save_env(
    *,
    cookie: str | None = None,
    email: str | None = None,
    password: str | None = None,
    clear_cookie: bool = False,
) -> None:
    cur = _read_env_map()
    if clear_cookie:
        cur.pop("cookie", None)
    if cookie is not None:
        cur["cookie"] = _normalize_cookie(cookie)
    if email is not None:
        cur["email"] = email
    if password is not None:
        if password == "":
            cur.pop("password", None)
        else:
            cur["password"] = password
    cfg = load_config()
    if cfg.get("character_id"):
        cur["character_id"] = str(cfg["character_id"])
    if cfg.get("project_path"):
        cur["project_path"] = str(cfg["project_path"])
    lines: list[str] = []
    if cur.get("email"):
        lines.append(f"email={cur['email']}")
    if cur.get("password"):
        lines.append(f"password={cur['password']}")
    if cur.get("cookie"):
        lines.append(f"cookie={_normalize_cookie(cur['cookie'])}")
    for k, v in cur.items():
        if k in ("email", "password", "cookie"):
            continue
        lines.append(f"{k}={v}")
    _atomic_write_text(ENV_PATH, "\n".join(lines) + "\n")


def _auth_request(url: str, *, method: str = "GET", data=None, cookie: str | None = None, referer: str | None = None):
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    origin = get_origin()
    headers = {
        "User-Agent": "Mozilla/5.0 DZMM-Studio-Bridge",
        "Accept": "application/json",
        "Origin": origin,
        "Referer": referer or f"{origin}/",
        "x-dzmm-request-id": f"studio{int(time.time()) % 10_000_000}",
    }
    if cookie:
        headers["Cookie"] = _normalize_cookie(cookie)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, raw, jar
    except urllib.error.HTTPError as e:
        return e.code, e.read(), jar


def refresh_session_cookie(cookie: str) -> str:
    """Renew access via GET /api/auth/token (uses refresh_token inside cookie)."""
    st, raw, jar = _auth_request(f"{get_origin()}/api/auth/token", method="GET", cookie=cookie)
    new_cookie = _cookie_from_jar(jar)
    if new_cookie:
        return new_cookie
    if st != 200:
        raise RuntimeError(f"refresh failed HTTP {st}: {raw[:200]!r}")
    # some responses only return access_token JSON; keep old cookie if jar empty
    try:
        data = json.loads(raw)
        tok = _session_from_cookie(cookie)
        if data.get("access_token"):
            tok["access_token"] = data["access_token"]
        if data.get("expires_at"):
            tok["expires_at"] = int(data["expires_at"])
            tok["expires_in"] = max(0, int(data["expires_at"]) - int(time.time()))
        raw_b64 = base64.b64encode(json.dumps(tok, separators=(",", ":")).encode()).decode().rstrip("=")
        return f"sb-rls-auth-token=base64-{raw_b64}"
    except Exception as e:
        raise RuntimeError(f"refresh parse failed: {e}") from e


def login_with_password(email: str, password: str) -> str:
    st, raw, jar = _auth_request(
        f"{get_origin()}/api/auth/sign-in",
        method="POST",
        data={"email": email.strip(), "password": password},
        referer=f"{get_origin()}/sign-in",
    )
    new_cookie = _cookie_from_jar(jar)
    if new_cookie:
        return new_cookie
    err = raw[:300]
    try:
        j = json.loads(raw)
        err = j.get("error") or j.get("message") or err
    except Exception:
        pass
    raise RuntimeError(f"login failed HTTP {st}: {err}")


def login_with_cookie(cookie: str) -> str:
    """粘贴浏览器 Cookie / sb-rls-auth-token 值，校验并可自动 refresh。"""
    c = _normalize_cookie(cookie or "")
    if not c:
        raise RuntimeError("Cookie 为空：请粘贴 sb-rls-auth-token=base64-... 或整段 Cookie 头")
    try:
        tok = _session_from_cookie(c)
    except Exception as e:
        raise RuntimeError(f"Cookie 无法解析：{e}") from e
    remain = int(tok.get("expires_at", 0) - time.time())
    if remain < AUTH_REFRESH_SKEW:
        try:
            return refresh_session_cookie(c)
        except Exception as e:
            if remain > 30:
                return c
            raise RuntimeError(f"Cookie 已过期且刷新失败：{e}") from e
    # 顺带打一下 token 接口，确认线路可用；失败仍可先收下本地 cookie
    try:
        return refresh_session_cookie(c)
    except Exception:
        return c


def _multipart_body(fields: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """fields: name -> (filename, content, content_type)"""
    boundary = f"----DZMMFormBoundary{int(time.time() * 1000)}"
    chunks: list[bytes] = []
    for name, (filename, content, ctype) in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        disp = f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        chunks.append(disp.encode())
        chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode())
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def login_with_signin_code_image(image_bytes: bytes, filename: str = "sign-in-code.jpg") -> str:
    """官方「使用登录码登录」：上传登录码截图/二维码图 → POST /api/auth/sign-in-code/scan。"""
    if not image_bytes:
        raise RuntimeError("请上传登录码图片")
    name = (filename or "sign-in-code.jpg").strip() or "sign-in-code.jpg"
    lower = name.lower()
    if lower.endswith(".png"):
        ctype = "image/png"
    elif lower.endswith(".webp"):
        ctype = "image/webp"
    elif lower.endswith(".gif"):
        ctype = "image/gif"
    else:
        ctype = "image/jpeg"
        if not lower.endswith((".jpg", ".jpeg")):
            name = "sign-in-code.jpg"
    body, content_type = _multipart_body({"image": (name, image_bytes, ctype)})
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    origin = get_origin()
    headers = {
        "User-Agent": "Mozilla/5.0 DZMM-Studio-Bridge",
        "Accept": "application/json",
        "Origin": origin,
        "Referer": f"{origin}/sign-in",
        "Content-Type": content_type,
        "x-dzmm-request-id": f"studio{int(time.time()) % 10_000_000}",
    }
    req = urllib.request.Request(
        f"{origin}/api/auth/sign-in-code/scan",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with opener.open(req, timeout=90) as resp:
            st, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        st, raw = e.code, e.read()
    new_cookie = _cookie_from_jar(jar)
    if new_cookie:
        return new_cookie
    err = raw[:300]
    try:
        j = json.loads(raw.decode("utf-8", "replace"))
        err = j.get("error") or j.get("message") or err
    except Exception:
        pass
    raise RuntimeError(f"登录码登录失败 HTTP {st}: {err}")


def tg_create_sign_in_code() -> dict:
    """创建 Telegram 登录码（二维码 / bot 指令）。"""
    st, raw, _jar = _auth_request(
        f"{get_origin()}/api/auth/tg-sign-in-code",
        method="GET",
        referer=f"{get_origin()}/sign-in",
    )
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception as e:
        raise RuntimeError(f"Telegram 取码失败 HTTP {st}: {raw[:200]!r}") from e
    if st != 200 or not isinstance(data, dict) or not data.get("signInCode"):
        msg = data.get("error") or data.get("message") if isinstance(data, dict) else raw[:200]
        raise RuntimeError(f"Telegram 取码失败 HTTP {st}: {msg}")
    return {
        "signInCode": data.get("signInCode"),
        "botUsername": data.get("botUsername") or "",
        "qrCodeUrl": data.get("qrCodeUrl") or "",
        "qrCodeSvg": data.get("qrCodeSvg") or "",
        "createdAt": data.get("createdAt") or "",
    }


def tg_poll_sign_in_code(sign_in_code: str) -> dict:
    """轮询 Telegram 登录状态；logged_in 时返回 cookie。"""
    code = (sign_in_code or "").strip()
    if not code:
        raise RuntimeError("缺少 signInCode")
    st, raw, jar = _auth_request(
        f"{get_origin()}/api/auth/tg-sign-in-code/{urllib.parse.quote(code)}",
        method="GET",
        referer=f"{get_origin()}/sign-in",
    )
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception as e:
        raise RuntimeError(f"Telegram 轮询失败 HTTP {st}: {raw[:200]!r}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Telegram 轮询异常 HTTP {st}")
    status = str(data.get("status") or "")
    cookie = _cookie_from_jar(jar)
    out = {
        "status": status,
        "message": data.get("message") or "",
        "cookie": cookie,
        "loggedIn": status == "logged_in" and bool(cookie),
    }
    if status == "logged_in" and not cookie:
        # 偶发只回 JSON、cookie 已在先前请求：再试 token 无 jar 仍空则报错
        out["loggedIn"] = False
        out["message"] = out["message"] or "已确认登录但未拿到 cookie，请改用 Cookie 粘贴或邮箱密码"
    return out


def load_auth(min_remain: int = 60):
    env = _read_env_map()
    email = (env.get("email") or "").strip()
    password = env.get("password") or ""
    cookie = _normalize_cookie(env.get("cookie") or "")

    def _ok(c: str):
        tok = _session_from_cookie(c)
        remain = int(tok.get("expires_at", 0) - time.time())
        return c, tok["access_token"], remain, tok.get("user", {}).get("email") or email or "?"

    # 1) try existing cookie + auto refresh
    if cookie:
        try:
            c, token, remain, mail = _ok(cookie)
            if remain >= max(min_remain, AUTH_REFRESH_SKEW):
                return c, token, remain, mail
            # near expiry / expired → refresh via token API
            try:
                refreshed = refresh_session_cookie(c)
                mail = email or mail
                _save_env(cookie=refreshed, email=mail if mail and mail != "?" else None, password=password or None)
                print(f"[auth] cookie refreshed, remain≈{_ok(refreshed)[2]}s", file=sys.stderr)
                return _ok(refreshed)
            except Exception as e:
                print(f"[auth] cookie refresh failed: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[auth] cookie invalid: {e}", file=sys.stderr)

    # 2) email + password login
    if email and password:
        print(f"[auth] signing in as {email} …", file=sys.stderr)
        fresh = login_with_password(email, password)
        _save_env(cookie=fresh, email=email, password=password)
        return _ok(fresh)

    raise SystemExit(
        "未登录：请在控制台用「邮箱密码 / 登录码 / Telegram / Cookie」登录，"
        "或在 .env 写 email+password，或 cookie=sb-rls-auth-token=base64-..."
    )


def _is_transient_http_error(exc: BaseException) -> bool:
    """CDN / 容器代理掐 TLS、空闲断开、超时：应重试，不是业务失败。"""
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return int(getattr(exc, "code", 0) or 0) in HTTP_RETRY_STATUSES
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            BrokenPipeError,
            InterruptedError,
            ssl.SSLError,
            http_client.IncompleteRead,
            http_client.RemoteDisconnected,
            http_client.BadStatusLine,
        ),
    ):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, BaseException) and _is_transient_http_error(reason):
            return True
        text = f"{exc} {reason or ''}".lower()
        return any(m in text for m in _TRANSIENT_NET_MARKERS)
    if isinstance(exc, OSError):
        text = str(exc).lower()
        return any(m in text for m in _TRANSIENT_NET_MARKERS)
    text = str(exc).lower()
    if any(m in text for m in ("http 408", "http 429", "http 500", "http 502", "http 503", "http 504")):
        return True
    return any(m in text for m in _TRANSIENT_NET_MARKERS)


def _sleep_http_retry(attempt: int) -> None:
    delay = min(8.0, 0.45 * (2 ** max(0, attempt - 1)))
    delay += random.uniform(0, 0.25)
    time.sleep(delay)


def http(url, cookie, token, method="GET", data=None, raw_body=None, content_type=None, timeout=120, accept="*/*", connection_close: bool = True):
    """Workbench / 代理 HTTP。SSL EOF、502 等瞬时失败自动退避重试。"""
    last_exc: BaseException | None = None
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        headers = {
            "Cookie": cookie,
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 DZMM-Studio-Bridge",
            "Accept": accept,
            "Referer": f"{get_origin()}/studio/game-creation/workbench?character_id={DEFAULT_CARD_ID}",
            "Origin": get_origin(),
            "x-dzmm-request-id": f"studio{int(time.time() * 1000) % 10_000_000}-{attempt}",
        }
        if connection_close:
            headers["Connection"] = "close"
        body = None
        if raw_body is not None:
            body = raw_body
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            hdrs = dict(e.headers or {})
            if int(e.code) in HTTP_RETRY_STATUSES and attempt < HTTP_RETRY_ATTEMPTS:
                last_exc = e
                _sleep_http_retry(attempt)
                continue
            return e.code, raw, hdrs
        except Exception as e:
            last_exc = e
            if attempt >= HTTP_RETRY_ATTEMPTS or not _is_transient_http_error(e):
                raise
            _sleep_http_retry(attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("http retry exhausted")


def ensure_editor(cookie, token, character_id: int):
    st, raw, _ = http(
        f"{get_origin()}/api/gamefy/editor",
        cookie,
        token,
        method="POST",
        data={"characterId": character_id},
        timeout=180,
        accept="application/json",
    )
    if st != 200:
        raise SystemExit(f"创建/连接编辑器失败 HTTP {st}: {raw[:300]!r}")
    info = json.loads(raw)
    if "needsTemplate" in info:
        raise SystemExit("该角色需要先在网页 workbench 选择模板")
    game_id = str(info.get("gameId") or character_id)
    # keep container warm
    http(
        f"{get_origin()}/api/game-studio/proxy/{game_id}/heartbeat",
        cookie,
        token,
        method="POST",
        data={},
        timeout=60,
        accept="application/json",
    )
    return info, game_id


def proxy_url(game_id: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{get_origin()}/api/game-studio/proxy/{game_id}{path}"


def _project_sync_skip() -> tuple[set[str], list[str]]:
    path = ROOT / "_sync_skip.txt"
    if not path.is_file():
        return set(), []
    exact: set[str] = set()
    prefixes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        norm = line.replace("\\", "/")
        if norm.endswith("/"):
            prefixes.append(norm)
        else:
            exact.add(norm)
    return exact, prefixes


def list_local_files() -> list[Path]:
    refresh_root()
    extra_skip, extra_prefixes = _project_sync_skip()
    files: list[Path] = []
    for pattern in SYNC_GLOBS:
        for path in ROOT.glob(pattern):
            if not path.is_file():
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            rel_skip = path.relative_to(ROOT).as_posix()
            if rel_skip in SYNC_SKIP_REL or rel_skip in extra_skip:
                continue
            if any(rel_skip.startswith(p) for p in extra_prefixes):
                continue
            files.append(path)
    # unique
    return sorted(set(files), key=lambda p: p.as_posix())


def sync_meta_path() -> Path:
    refresh_root()
    return ROOT / SYNC_META_NAME


def _file_sig(path: Path) -> dict:
    st = path.stat()
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
    return {
        "size": int(st.st_size),
        "mtime_ns": mtime_ns,
        "sha256": h.hexdigest(),
    }


def load_sync_meta() -> dict:
    path = sync_meta_path()
    if not path.is_file():
        return {"version": 1, "files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            data.setdefault("version", 1)
            return data
    except Exception:
        pass
    return {"version": 1, "files": {}}


def save_sync_meta(meta: dict) -> None:
    path = sync_meta_path()
    out = {
        "version": 1,
        "character_id": int(meta.get("character_id") or DEFAULT_CARD_ID or 0),
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": meta.get("files") if isinstance(meta.get("files"), dict) else {},
    }
    if meta.get("gitSavePending"):
        out["gitSavePending"] = True
    _atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2) + "\n")


def write_sync_baseline(files: list[Path] | None = None) -> dict:
    """把当前本地文件记为已同步基线（不上传）。拉取完成后应调用。"""
    refresh_root()
    files = list(files) if files is not None else list_local_files()
    entries: dict = {}
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            entries[rel] = _file_sig(path)
        except Exception:
            continue
    meta = {
        "version": 1,
        "character_id": DEFAULT_CARD_ID,
        "files": entries,
    }
    save_sync_meta(meta)
    return meta


def _pull_meta_character_id() -> int:
    path = ROOT / "_pull_meta.json"
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return int(data.get("character_id") or 0)
    except Exception:
        pass
    return 0


def select_files_to_sync(
    files: list[Path],
    *,
    full: bool = False,
    character_id: int | None = None,
) -> tuple[list[Path], dict, str]:
    """选出需要上传的文件。

    - full=True：全部上传
    - character_id 与 `_sync_meta.json` 记录不一致（含旧 meta 缺 ID）：换卡全量
    - 无基线：仅当 `_pull_meta.json` 同卡时只建基线；否则全量上传
    - 有基线：只返回 size/mtime/sha256 变化的文件
    """
    refresh_root()
    files = list(files)
    meta = load_sync_meta()
    prev = dict(meta.get("files") or {})
    target_cid = int(
        character_id if character_id is not None else (DEFAULT_CARD_ID or 0)
    )
    meta_cid = int(meta.get("character_id") or 0)
    # 换卡，或旧基线没有 character_id：本地哈希未变也不能当“已同步到当前卡”
    if target_cid and prev and ((meta_cid and target_cid != meta_cid) or not meta_cid):
        meta = dict(meta)
        meta["character_id"] = target_cid
        return files, meta, "retarget"
    if full:
        return files, meta, "full"
    if not prev:
        pull_cid = _pull_meta_character_id()
        if target_cid and pull_cid and pull_cid == target_cid:
            write_sync_baseline(files)
            return [], load_sync_meta(), "baseline"
        # 未拉取过（或 pull 不是当前卡）：空基线不能当成已同步
        return files, {"version": 1, "character_id": target_cid, "files": {}}, "full"
    changed: list[Path] = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            st = path.stat()
        except OSError:
            continue
        old = prev.get(rel)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        if (
            old
            and int(old.get("size") or -1) == int(st.st_size)
            and int(old.get("mtime_ns") or -1) == mtime_ns
        ):
            continue
        try:
            sig = _file_sig(path)
        except Exception:
            changed.append(path)
            continue
        if old and old.get("sha256") and old.get("sha256") == sig["sha256"]:
            prev[rel] = sig
            continue
        changed.append(path)
    meta["files"] = prev
    return changed, meta, "incremental"


def to_container_path(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    return "/" + rel


def upload_file(cookie, token, game_id: str, local: Path) -> None:
    remote = to_container_path(local)
    data = local.read_bytes()
    if not data:
        data = b"\n"
    q = urllib.parse.urlencode({"path": remote, "createDirectories": "true"})
    st, raw, _ = http(
        proxy_url(game_id, f"/files/upload?{q}"),
        cookie,
        token,
        method="PUT",
        raw_body=data,
        content_type="application/octet-stream",
        timeout=180,
    )
    if st not in (200, 201, 204):
        raise RuntimeError(f"upload failed {remote} HTTP {st}: {raw[:200]!r}")


def delete_file(cookie, token, game_id: str, remote: str) -> None:
    """Delete a file (or empty path) in the Workbench container."""
    remote = "/" + str(remote or "").replace("\\", "/").lstrip("/")
    while "//" in remote:
        remote = remote.replace("//", "/")
    if remote in ("", "/"):
        raise ValueError("refuse delete container root")
    q = urllib.parse.urlencode({"path": remote})
    st, raw, _ = http(
        proxy_url(game_id, f"/files?{q}"),
        cookie,
        token,
        method="DELETE",
        accept="application/json",
        timeout=60,
    )
    if st not in (200, 204):
        raise RuntimeError(f"delete failed {remote} HTTP {st}: {raw[:200]!r}")


def _list_remote_dir(cookie, token, game_id: str, path: str) -> list[dict]:
    q = urllib.parse.urlencode({"path": path})
    st, raw, _ = http(
        proxy_url(game_id, f"/files?{q}"),
        cookie,
        token,
        accept="application/json",
        timeout=60,
    )
    if st != 200:
        raise RuntimeError(f"list remote {path} HTTP {st}: {raw[:200]!r}")
    data = json.loads(raw)
    return data if isinstance(data, list) else []


def list_remote_files(cookie, token, game_id: str, path: str = "/") -> list[dict]:
    """Walk container files (skip .git / node_modules / __pycache__)."""
    skip_names = {".git", "node_modules", "__pycache__", ".DS_Store"}
    out: list[dict] = []

    def walk(cur: str) -> None:
        for e in _list_remote_dir(cookie, token, game_id, cur):
            name = e.get("name") or ""
            if name in skip_names:
                continue
            p = str(e.get("path") or "").replace("\\", "/")
            if not p.startswith("/"):
                p = "/" + p
            t = e.get("type")
            if t == "directory":
                walk(p)
            elif t == "file":
                out.append({"path": p, "size": int(e.get("size") or 0), "name": name})

    walk(path if path.startswith("/") else "/" + path)
    return out


def prune_remote_extras(
    cookie,
    token,
    game_id: str,
    local_files: list[Path] | None = None,
) -> dict:
    """Delete container files that are not present in the local sync set.

    Local project is the source of truth. Returns counts + sample paths.
    列目录走代理 HTTPS：上传刚结束时 CDN/容器常直接掐 TLS（UNEXPECTED_EOF），
    所以列目录本身会退避重试，避免清理阶段把整次同步打断。
    """
    refresh_root()
    local_files = list(local_files) if local_files is not None else list_local_files()
    keep = {"/" + p.relative_to(ROOT).as_posix() for p in local_files}
    # 上传刚结束，容器还在落盘；立刻 GET /files 最容易撞上对端关连接
    time.sleep(0.4)
    remote = None
    last_err: BaseException | None = None
    for attempt in range(1, 4):
        try:
            remote = list_remote_files(cookie, token, game_id, "/")
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt >= 3 or not _is_transient_http_error(e):
                raise
            _sleep_http_retry(attempt + 1)
    if remote is None:
        raise last_err or RuntimeError("list remote files failed")
    extras = [f for f in remote if f.get("path") not in keep]
    deleted: list[str] = []
    fails: list[str] = []
    for f in extras:
        remote_path = f["path"]
        try:
            delete_file(cookie, token, game_id, remote_path)
            deleted.append(remote_path)
        except Exception as e:
            fails.append(f"{remote_path}: {e}")
    return {
        "remote": len(remote),
        "keep": len(keep),
        "deleted": deleted,
        "fails": fails,
        "ok": len(deleted),
        "fail": len(fails),
    }


def git_save(cookie, token, game_id: str, message: str | None) -> None:
    st, raw, _ = http(
        proxy_url(game_id, "/git/save"),
        cookie,
        token,
        method="POST",
        data={"message": message or None},
        timeout=120,
        accept="application/json",
    )
    if st not in (200, 201):
        raise SystemExit(f"git save 失败 HTTP {st}: {raw[:300]!r}")


def publish(cookie, token, character_id: int, on_progress=None) -> dict:
    """发布到玩家静态包。接口返回 NDJSON 进度流，必须读到上传结束，
    否则会提前断开，玩家侧大量 JS/图 404。
    on_progress(ev) 可选：每条进度回调，ev 含 step/uploaded/total/current/message。"""
    url = f"{get_origin()}/api/gamefy/publish"
    last: dict = {}
    uploaded = 0
    total = 0
    last_exc: BaseException | None = None
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        headers = {
            "Cookie": cookie,
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 DZMM-Studio-Bridge",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Referer": f"{get_origin()}/studio/game-creation/workbench?character_id={character_id}",
            "Origin": get_origin(),
            "x-dzmm-request-id": f"studio{int(time.time() * 1000) % 10_000_000}-{attempt}",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps({"characterId": character_id}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            log_path = KIT_ROOT / "tools" / "_last_publish.ndjson"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(req, timeout=600) as resp, log_path.open("w", encoding="utf-8") as logf:
                st = int(resp.status)
                if st not in (200, 201):
                    raw = resp.read()[:400]
                    raise SystemExit(f"发布失败 HTTP {st}: {raw!r}")
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    logf.write(text + "\n")
                    logf.flush()
                    try:
                        obj = json.loads(text)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    last = obj
                    uploaded = int(obj.get("uploaded") or uploaded or 0)
                    total = int(obj.get("total") or total or 0)
                    step = str(obj.get("step") or "")
                    current = str(obj.get("current") or "").strip()
                    if callable(on_progress):
                        try:
                            on_progress({
                                "step": step or ("uploading" if total else ""),
                                "uploaded": uploaded,
                                "total": total,
                                "current": current,
                                "message": str(obj.get("message") or ""),
                                "heartbeat": bool(obj.get("heartbeat")),
                            })
                        except Exception:
                            pass
                    if step == "uploading" and total and (uploaded % 25 == 0 or uploaded == total):
                        print(f"[publish] upload {uploaded}/{total}", flush=True)
                    elif step and step not in ("uploading",):
                        print(f"[publish] {step} {obj.get('message') or ''}", flush=True)
                    if (
                        obj.get("failed")
                        or obj.get("failCount")
                        or obj.get("failedCount")
                        or obj.get("failedFiles")
                        or obj.get("fails")
                        or obj.get("errors")
                        or obj.get("failedList")
                    ):
                        print("[publish] fail-detail", json.dumps(obj, ensure_ascii=False)[:6000], flush=True)
            break
        except urllib.error.HTTPError as e:
            raw = e.read()[:400]
            if int(e.code) in HTTP_RETRY_STATUSES and attempt < HTTP_RETRY_ATTEMPTS:
                last_exc = e
                _sleep_http_retry(attempt)
                continue
            raise SystemExit(f"发布失败 HTTP {e.code}: {raw!r}") from e
        except Exception as e:
            last_exc = e
            if attempt >= HTTP_RETRY_ATTEMPTS or not _is_transient_http_error(e):
                raise
            print(f"[publish] retry {attempt} after {e}", flush=True)
            _sleep_http_retry(attempt)
    else:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("publish retry exhausted")

    step = str(last.get("step") or "")
    err = last.get("error") or (last.get("message") if step in ("error", "failed") else "")
    if step in ("error", "failed") or last.get("ok") is False:
        dump = KIT_ROOT / "tools" / "_last_publish_error.json"
        try:
            dump.write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        print("[publish] last=", json.dumps(last, ensure_ascii=False)[:4000], flush=True)
        raise SystemExit(f"发布失败: {err or last}")
    if total and uploaded < total:
        raise SystemExit(
            f"发布中断：只上传了 {uploaded}/{total} 个文件，玩家包会缺脚本。"
            "请再点一次发布，并等进度走完。"
        )
    print(f"[publish] done uploaded={uploaded}/{total or uploaded} step={step or 'eof'}", flush=True)
    return last


def cmd_login(args):
    env = _read_env_map()
    if getattr(args, "cookie", None):
        fresh = login_with_cookie(args.cookie)
        mail = ""
        try:
            mail = str((_session_from_cookie(fresh).get("user") or {}).get("email") or "")
        except Exception:
            pass
        _save_env(cookie=fresh, email=mail or None)
        _, _, remain, mail2 = load_auth(min_remain=0)
        print(f"login ok (cookie) email={mail2 or mail} remain_s={remain}")
        return
    if getattr(args, "code_image", None):
        path = Path(args.code_image)
        if not path.is_file():
            raise SystemExit(f"找不到登录码图片: {path}")
        fresh = login_with_signin_code_image(path.read_bytes(), path.name)
        mail = ""
        try:
            mail = str((_session_from_cookie(fresh).get("user") or {}).get("email") or "")
        except Exception:
            pass
        _save_env(cookie=fresh, email=mail or None)
        _, _, remain, mail2 = load_auth(min_remain=0)
        print(f"login ok (登录码) email={mail2 or mail} remain_s={remain}")
        return
    email = (args.email or env.get("email") or "").strip()
    password = args.password if args.password is not None else (env.get("password") or "")
    if not email or not password:
        raise SystemExit(
            "用法:\n"
            "  python lib/dzmm_studio.py login --email you@mail.com --password '***'\n"
            "  python lib/dzmm_studio.py login --cookie 'sb-rls-auth-token=...'\n"
            "  python lib/dzmm_studio.py login --code-image sign-in.png\n"
            "或先在 .env 写好 email= / password=；Telegram 请在网页控制台登录。"
        )
    fresh = login_with_password(email, password)
    _save_env(cookie=fresh, email=email, password=password)
    _, _, remain, mail = load_auth()
    print(f"login ok email={mail} remain_s={remain}")
    print(".env 已写入 cookie（并保留 email/password，供以后自动续期/重登）")


def cmd_status(args):
    cookie, token, remain, email = load_auth()
    print(f"login={email} remain_s={remain}")
    info, game_id = ensure_editor(cookie, token, args.character_id)
    print(f"editor status={info.get('status')} gameId={game_id} container={str(info.get('containerId'))[:16]}…")
    st, raw, _ = http(proxy_url(game_id, "/git/status"), cookie, token, accept="application/json")
    print("git", raw.decode("utf-8", "replace")[:500])
    st, raw, _ = http(proxy_url(game_id, "/publish/files"), cookie, token, accept="application/json")
    files = json.loads(raw).get("files") or []
    print(f"publish_files={len(files)}")
    print(f"workbench={get_origin()}/studio/game-creation/workbench?character_id={args.character_id}")


def cmd_sync(args):
    cookie, token, remain, email = load_auth()
    print(f"login={email} remain_s={remain}")
    _, game_id = ensure_editor(cookie, token, args.character_id)
    all_local = list_local_files()
    partial = bool(args.only)
    files = all_local
    if args.only:
        only = {p.replace("\\", "/") for p in args.only}
        files = [f for f in files if f.relative_to(ROOT).as_posix() in only]
    full = bool(getattr(args, "full", False))
    to_upload, meta, mode = select_files_to_sync(
        files, full=full, character_id=int(args.character_id or 0)
    )
    if mode == "baseline":
        print(f"sync: 已建立增量基线（{len((meta.get('files') or {}))} 个文件），本次不上传")
        print("提示：之后只传有改动的文件；若需全量请加 --full")
        return
    git_pending = bool(meta.get("gitSavePending"))
    if mode == "retarget":
        print(
            f"sync: 检测到目标卡 ID 变更，强制全量上传 "
            f"{len(to_upload)} 个文件 -> gameId={game_id}"
        )
    elif to_upload:
        print(f"sync {len(to_upload)}/{len(files)} changed -> container gameId={game_id} ({mode})")
    elif git_pending:
        print("sync: 无文件变更，补做上次未完成的 git save")
    ok = fail = 0
    entries = {} if mode in ("full", "retarget") else dict(meta.get("files") or {})
    for i, path in enumerate(to_upload, 1):
        rel = path.relative_to(ROOT).as_posix()
        try:
            upload_file(cookie, token, game_id, path)
            entries[rel] = _file_sig(path)
            ok += 1
            if args.verbose or i % 25 == 0 or i == len(to_upload):
                print(f"  [{i}/{len(to_upload)}] {rel}")
        except Exception as e:
            fail += 1
            entries.pop(rel, None)
            print(f"  FAIL {rel}: {e}")
    keep_rels = {p.relative_to(ROOT).as_posix() for p in all_local}
    if to_upload:
        entries = {k: v for k, v in entries.items() if k in keep_rels}
        meta["files"] = entries
        meta["character_id"] = int(getattr(args, "character_id", 0) or DEFAULT_CARD_ID or 0)
        save_sync_meta(meta)
        print(f"uploaded ok={ok} fail={fail}")
        if fail:
            raise SystemExit(1)
    else:
        prev = dict(meta.get("files") or {})
        cleaned = {k: v for k, v in prev.items() if k in keep_rels}
        if cleaned != prev:
            meta["files"] = cleaned
            meta["character_id"] = int(getattr(args, "character_id", 0) or DEFAULT_CARD_ID or 0)
            save_sync_meta(meta)

    deleted_n = 0
    if not partial:
        print("prune remote extras…")
        try:
            prune = prune_remote_extras(cookie, token, game_id, all_local)
        except Exception as e:
            # 文件已在容器工作区；清理失败不应跳过 git save
            print(f"prune failed (will git save anyway): {e}")
            prune = {"ok": 0, "fails": [str(e)], "deleted": []}
        deleted_n = int(prune.get("ok") or 0)
        for p in (prune.get("deleted") or [])[:20]:
            print(f"  DEL {p}")
        if deleted_n > 20:
            print(f"  … and {deleted_n - 20} more")
        for line in prune.get("fails") or []:
            print(f"  DEL FAIL {line}")
        print(f"pruned deleted={deleted_n} fail={prune.get('fail') or len(prune.get('fails') or [])}")

    need_git = bool(to_upload or deleted_n or git_pending)
    if not need_git:
        print("sync: 无变更文件，跳过")
        return
    if not args.no_git_save:
        print("git save…")
        try:
            git_save(cookie, token, game_id, args.message)
            meta.pop("gitSavePending", None)
            meta["character_id"] = int(getattr(args, "character_id", 0) or DEFAULT_CARD_ID or 0)
            if to_upload:
                meta["files"] = entries
            save_sync_meta(meta)
            print("git save done")
        except Exception as e:
            meta["gitSavePending"] = True
            meta["character_id"] = int(getattr(args, "character_id", 0) or DEFAULT_CARD_ID or 0)
            if to_upload:
                meta["files"] = entries
            save_sync_meta(meta)
            raise SystemExit(f"git save 失败（已标记待重试）: {e}") from e
    else:
        print("skip git save (--no-git-save)")


def cmd_publish(args):
    cookie, token, remain, email = load_auth()
    print(f"login={email} remain_s={remain}")
    ensure_editor(cookie, token, args.character_id)
    if not args.yes:
        ans = input(f"确认发布 character_id={args.character_id} 到线上？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            raise SystemExit("已取消")
    print("publishing…")
    publish(cookie, token, args.character_id)
    print("publish ok")
    print(f"play/check: {get_origin()}/character/{args.character_id}")
    print(f"workbench: {get_origin()}/studio/game-creation/workbench?character_id={args.character_id}")


def cmd_deploy(args):
    # sync + publish
    args.no_git_save = False
    cmd_sync(args)
    args.yes = True if args.yes else args.yes
    if not args.yes:
        ans = input("同步完成，继续发布到线上？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            raise SystemExit("已同步，未发布")
        args.yes = True
    cmd_publish(args)


def cmd_preview(args):
    # local static preview of publish/
    import http.server
    import socketserver
    import webbrowser

    publish_dir = ROOT / "publish"
    if not (publish_dir / "index.html").exists():
        raise SystemExit("缺少 publish/index.html")
    port = args.port
    handler = http.server.SimpleHTTPRequestHandler
    os_chdir = __import__("os").chdir
    os_chdir(publish_dir)

    class Quiet(handler):
        def log_message(self, fmt, *a):
            if args.verbose:
                super().log_message(fmt, *a)

    with socketserver.TCPServer(("127.0.0.1", port), Quiet) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"local publish preview: {url}")
        print("说明：这是本地静态预览，不含完整 Workbench 容器/SDK。")
        print(f"线上开发端预览请打开: {get_origin()}/studio/game-creation/workbench?character_id={args.character_id}")
        print("  → Preview 面板刷新即可看到已 sync 的容器内容")
        if not args.no_open:
            webbrowser.open(url)
            webbrowser.open(f"{get_origin()}/studio/game-creation/workbench?character_id={args.character_id}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


def main():
    refresh_root()
    p = argparse.ArgumentParser(description="DZMM Studio local bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--character-id", type=int, default=DEFAULT_CARD_ID or 0)

    s = sub.add_parser("login", help="登录并写入 .env cookie（邮箱密码 / Cookie / 登录码图）")
    s.add_argument("--email")
    s.add_argument("--password")
    s.add_argument("--cookie", help="粘贴 sb-rls-auth-token=… 或浏览器 Cookie")
    s.add_argument("--code-image", help="官方登录码截图/二维码图片路径")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("status", help="检查登录态与远程容器")
    add_common(s)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("sync", help="增量同步本地改动到线上容器并 git save（默认只传变更）")
    add_common(s)
    s.add_argument("--message", default="sync from local")
    s.add_argument("--no-git-save", action="store_true")
    s.add_argument("--full", action="store_true", help="强制全量上传（忽略增量基线）")
    s.add_argument("--only", nargs="*", help="只同步指定相对路径")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("publish", help="触发线上「保存游戏」发布")
    add_common(s)
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("deploy", help="sync + publish")
    add_common(s)
    s.add_argument("--message", default="deploy from local")
    s.add_argument("-y", "--yes", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--only", nargs="*")
    s.set_defaults(func=cmd_deploy)

    s = sub.add_parser("preview", help="本地静态预览 + 打开线上 workbench")
    add_common(s)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_preview)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
