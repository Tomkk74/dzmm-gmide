#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DZMM 本地开发控制台：Web 填写账号并登录。"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KIT = Path(__file__).resolve().parent
sys.path.insert(0, str(KIT / "lib"))
import dzmm_studio as studio  # noqa: E402
import dzmm_character as character  # noqa: E402
import dzmm_agent as agent  # noqa: E402
import pull_container as puller  # noqa: E402

WEB_DIR = KIT / "web"
DEFAULT_PORT = 8788

_PULL_LOCK = threading.Lock()
_PULL_JOB: dict = {
    "running": False,
    "phase": "",
    "message": "",
    "current": 0,
    "total": 0,
    "ok": 0,
    "fail": 0,
    "out": "",
    "error": "",
    "done": False,
    "result": None,
    "logs": [],
}

_PREVIEW_LOCK = threading.Lock()
_PREVIEW_PROC: subprocess.Popen | None = None
_PREVIEW_META: dict = {
    "running": False,
    "port": 8791,
    "url": "",
    "characterId": 0,
    "projectPath": "",
    "pid": 0,
    "error": "",
    "message": "",
    "source": "local",
}
_CLOUD_MIRROR_LOCK = threading.Lock()
_CLOUD_MIRROR: dict = {
    "enabled": False,
    "running": False,
    "stop": False,
    "characterId": 0,
    "lastAt": 0.0,
    "changedAt": 0.0,
    "revision": 0,
    "message": "",
    "error": "",
    "downloaded": 0,
    "skipped": 0,
    "intervalSec": 6,
}
_CLOUD_MIRROR_THREAD: threading.Thread | None = None
_CLOUD_MIRROR_GEN = 0
_PREVIEW_SOURCE = "local"

_SYNC_LOCK = threading.Lock()
_SYNC_JOB: dict = {
    "running": False,
    "phase": "",
    "message": "",
    "current": 0,
    "total": 0,
    "ok": 0,
    "fail": 0,
    "error": "",
    "done": False,
    "fails": [],
    "gameId": "",
}


_JOB_CLAIM_LOCK = threading.Lock()


def _project_job_busy() -> str:
    """Return 'sync' / 'pull' if a project-mutating job is running."""
    with _SYNC_LOCK:
        if _SYNC_JOB.get("running"):
            return "sync"
    with _PULL_LOCK:
        if _PULL_JOB.get("running"):
            return "pull"
    return ""


def _claim_project_job(kind: str, init: dict) -> str:
    """Atomically claim sync or pull. Returns '' on success, or busy kind."""
    with _JOB_CLAIM_LOCK:
        busy = _project_job_busy()
        if busy:
            return busy
        if kind == "sync":
            with _SYNC_LOCK:
                _SYNC_JOB.update(init)
        elif kind == "pull":
            with _PULL_LOCK:
                _PULL_JOB.update(init)
        else:
            return "busy"
        return ""


_CLIENT_DISCONNECT = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(raw)
    except _CLIENT_DISCONNECT:
        # 浏览器刷新/切页/取消轮询时会先断连接，响应写不完 — 正常，不必打栈
        pass


def _read_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _mask_email(email: str) -> str:
    email = (email or "").strip()
    if "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return name[:1] + "***@" + domain
    return name[:2] + "***@" + domain


def build_status() -> dict:
    studio.refresh_root()
    cfg = studio.load_config()
    env = studio._read_env_map()
    email = (env.get("email") or "").strip()
    has_password = bool(env.get("password"))
    logged_in = False
    remain = 0
    auth_email = ""
    error = ""
    try:
        if env.get("cookie") or (email and has_password):
            _cookie, _token, remain, auth_email = studio.load_auth(min_remain=0)
            logged_in = remain > 0
    except SystemExit as e:
        error = str(e)
    except Exception as e:
        error = str(e)

    project = str(studio.project_root())
    cid = int(cfg.get("character_id") or 0)
    project_path = project if project != str(studio.KIT_ROOT) else (cfg.get("project_path") or "")
    resolved = studio.resolve_game_project(project_path) if project_path else {
        "ok": False,
        "root": Path(),
        "hint": "",
        "error": "",
        "failedPaths": [],
    }
    if project_path and resolved.get("ok") and resolved.get("root"):
        project_path = str(resolved["root"])
    elif project_path:
        project_path = str(studio.normalize_project_root(project_path))
    return {
        "ok": True,
        "loggedIn": logged_in,
        "remainSec": max(0, int(remain)),
        "email": auth_email or email,
        "emailMasked": _mask_email(auth_email or email),
        "hasPassword": has_password,
        "characterId": cid,
        "projectPath": project_path,
        "previewPort": int(cfg.get("preview_port") or 8791),
        "origin": studio.get_origin(),
        "origins": studio.list_origin_choices(),
        "addrPage": studio.ADDR_PAGE,
        "workbenchUrl": (
            f"{studio.get_origin()}/studio/game-creation/workbench?character_id={cid}" if cid else ""
        ),
        "kitRoot": str(studio.KIT_ROOT),
        "publishIndexExists": bool(resolved.get("ok")),
        "projectResolveHint": resolved.get("hint") or "",
        "projectResolveError": resolved.get("error") or "",
        "pullFailedCount": len(resolved.get("failedPaths") or []),
        "error": error,
        "consoleMode": _console_mode_from_cfg(cfg),
        "preview": preview_snapshot(),
        "projectHistory": studio.list_project_history(cfg),
    }


def _console_mode_from_cfg(cfg: dict | None = None) -> str:
    raw = str((cfg or studio.load_config()).get("console_mode") or "game").strip().lower()
    return "card" if raw == "card" else "game"


def do_console_mode_get() -> dict:
    return {"ok": True, "mode": _console_mode_from_cfg(), "status": build_status()}


def do_console_mode_set(body: dict | None = None) -> dict:
    """切换控制台偏好：角色卡模式会停掉游戏预览，只保留登录态供写卡/试玩。"""
    body = body or {}
    mode = str(body.get("mode") or "").strip().lower()
    if mode not in ("card", "game"):
        return {"ok": False, "error": "mode 须为 card 或 game", "status": build_status()}
    studio.save_config({"console_mode": mode})
    preview = None
    if mode == "card":
        try:
            _stop_cloud_mirror()
        except Exception:
            pass
        global _PREVIEW_SOURCE
        _PREVIEW_SOURCE = "local"
        preview = do_stop_preview().get("preview")
    return {
        "ok": True,
        "mode": mode,
        "preview": preview,
        "message": "已切换到角色卡（游戏预览已停）" if mode == "card" else "已切换到游戏卡",
        "status": build_status(),
    }


def do_origin_get() -> dict:
    return {
        "ok": True,
        "origin": studio.get_origin(),
        "origins": studio.list_origin_choices(),
        "addrPage": studio.ADDR_PAGE,
        "status": build_status(),
    }


def do_origin_set(body: dict) -> dict:
    body = body or {}
    if body.get("auto") or body.get("probe"):
        result = studio.pick_fastest_origin(timeout=float(body.get("timeout") or 6))
        result["status"] = build_status()
        return result
    raw = str(body.get("origin") or "").strip()
    if not raw:
        return {"ok": False, "error": "缺少 origin", "status": build_status()}
    origin = studio.set_origin(raw, clear_cookie=True)
    return {
        "ok": True,
        "origin": origin,
        "message": f"已切换到 {origin}。换域后请重新登录。",
        "addrPage": studio.ADDR_PAGE,
        "status": build_status(),
    }


def _apply_login_side_config(body: dict) -> None:
    """登录请求里可顺带带上线路 / 项目字段。"""
    updates = {}
    character_id = body.get("characterId")
    project_path = body.get("projectPath")
    preview_port = body.get("previewPort")
    origin = body.get("origin")
    if character_id is not None and str(character_id).strip() != "":
        updates["character_id"] = int(character_id)
    if project_path is not None:
        updates["project_path"] = str(project_path).strip()
    if preview_port is not None and str(preview_port).strip() != "":
        updates["preview_port"] = int(preview_port)
    if origin is not None and str(origin).strip() != "":
        studio.set_origin(origin, clear_cookie=False)
        updates["origin"] = studio.get_origin()
    if updates:
        studio.save_config(updates)


def _login_success(email_hint: str | None = None) -> dict:
    studio.refresh_root()
    _c, _t, remain, mail = studio.load_auth(min_remain=0)
    # 换号后立刻让预览桥接重读 cookie / 昵称，避免「链接用户」卡住旧名
    bridge = None
    try:
        bridge = do_bridge_auth_reload()
        if not bridge.get("ok"):
            bridge = do_bridge_status(force=True)
    except Exception:
        try:
            bridge = do_bridge_status(force=True)
        except Exception:
            bridge = None
    out = {
        "ok": True,
        "email": mail or email_hint or "",
        "remainSec": remain,
        "status": build_status(),
    }
    if bridge:
        out["bridge"] = bridge.get("bridge")
        out["preview"] = bridge.get("preview")
    return out


def do_login(body: dict) -> dict:
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    save_password = bool(body.get("savePassword", True))
    _apply_login_side_config(body)

    env = studio._read_env_map()
    if not email:
        email = (env.get("email") or "").strip()
    if not password:
        password = env.get("password") or ""
    if not email or not password:
        return {"ok": False, "error": "请填写邮箱和密码（或改用登录码 / Telegram / Cookie）"}

    try:
        fresh = studio.login_with_password(email, password)
        studio._save_env(
            cookie=fresh,
            email=email,
            password=password if save_password else "",
        )
        return _login_success(email)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_login_cookie(body: dict) -> dict:
    _apply_login_side_config(body)
    cookie = str(body.get("cookie") or "").strip()
    if not cookie:
        return {"ok": False, "error": "请粘贴 Cookie（sb-rls-auth-token=… 或浏览器整段 Cookie）"}
    try:
        fresh = studio.login_with_cookie(cookie)
        # 尽量从 cookie 里抠邮箱
        mail = ""
        try:
            tok = studio._session_from_cookie(fresh)
            mail = str((tok.get("user") or {}).get("email") or "")
        except Exception:
            pass
        studio._save_env(cookie=fresh, email=mail or None)
        return _login_success(mail)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_login_code(body: dict) -> dict:
    """登录码图片：imageBase64 + 可选 filename。"""
    _apply_login_side_config(body)
    b64 = str(body.get("imageBase64") or "").strip()
    if not b64:
        return {"ok": False, "error": "请上传登录码图片"}
    if "," in b64 and b64.lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception as e:
        return {"ok": False, "error": f"图片解码失败：{e}"}
    filename = str(body.get("filename") or "sign-in-code.jpg").strip() or "sign-in-code.jpg"
    try:
        fresh = studio.login_with_signin_code_image(raw, filename)
        mail = ""
        try:
            tok = studio._session_from_cookie(fresh)
            mail = str((tok.get("user") or {}).get("email") or "")
        except Exception:
            pass
        studio._save_env(cookie=fresh, email=mail or None)
        return _login_success(mail)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_login_telegram_start(body: dict | None = None) -> dict:
    body = body or {}
    _apply_login_side_config(body)
    try:
        data = studio.tg_create_sign_in_code()
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_login_telegram_poll(body: dict) -> dict:
    code = str(body.get("signInCode") or "").strip()
    if not code:
        return {"ok": False, "error": "缺少 signInCode"}
    try:
        data = studio.tg_poll_sign_in_code(code)
        if data.get("loggedIn") and data.get("cookie"):
            fresh = data["cookie"]
            mail = ""
            try:
                tok = studio._session_from_cookie(fresh)
                mail = str((tok.get("user") or {}).get("email") or "")
            except Exception:
                pass
            studio._save_env(cookie=fresh, email=mail or None)
            out = _login_success(mail)
            out["loggedIn"] = True
            out["tgStatus"] = data.get("status")
            out["message"] = data.get("message") or ""
            return out
        return {
            "ok": True,
            "loggedIn": False,
            "tgStatus": data.get("status"),
            "message": data.get("message") or "",
            "status": build_status(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_save_config(body: dict) -> dict:
    updates = {}
    if "characterId" in body:
        updates["character_id"] = int(body.get("characterId") or 0)
    if "projectPath" in body:
        updates["project_path"] = str(body.get("projectPath") or "").strip()
    if "previewPort" in body:
        updates["preview_port"] = int(body.get("previewPort") or 8791)
    if "projectLabel" in body and str(body.get("projectLabel") or "").strip():
        updates["project_label"] = str(body.get("projectLabel") or "").strip()
    if "origin" in body and str(body.get("origin") or "").strip():
        # 换线路：清 cookie，需重新登录
        origin = studio.set_origin(body.get("origin"), clear_cookie=True)
        updates["origin"] = origin
    studio.save_config(updates)
    env_email = (studio._read_env_map().get("email") or "").strip()
    if body.get("email"):
        studio._save_env(email=str(body["email"]).strip())
    elif env_email:
        studio._save_env(email=env_email)
    studio.refresh_root()
    return {"ok": True, "status": build_status()}


def do_remove_project_history(body: dict) -> dict:
    try:
        cid = int(body.get("characterId") or 0)
    except Exception:
        cid = 0
    path = str(body.get("projectPath") or "").strip()
    if cid <= 0 or not path:
        return {"ok": False, "error": "缺少 characterId / projectPath", "status": build_status()}
    studio.remove_project_history_entry(character_id=cid, project_path=path)
    return {"ok": True, "status": build_status()}


def do_logout() -> dict:
    studio._save_env(clear_cookie=True, cookie=None)
    return {"ok": True, "status": build_status()}


def do_agent_ready(body: dict | None = None) -> dict:
    body = body or {}
    try:
        cid = body.get("characterId")
        ready = agent.ensure_ready(int(cid) if cid else None)
        return {"ok": True, **ready, "status": build_status()}
    except Exception as e:
        return {"ok": False, "error": str(e), "status": build_status()}


def do_agent_sessions(backend: str = "claude") -> dict:
    try:
        data = agent.list_sessions(backend=backend or "claude")
        data["status"] = build_status()
        return data
    except Exception as e:
        return {"ok": False, "error": str(e), "status": build_status()}


def do_agent_messages(session_id: str, backend: str = "claude") -> dict:
    try:
        data = agent.session_messages(session_id, backend=backend or "claude")
        data["status"] = build_status()
        return data
    except Exception as e:
        return {"ok": False, "error": str(e), "status": build_status()}


def do_agent_send(body: dict) -> dict:
    try:
        data = agent.send_prompt(
            str(body.get("prompt") or ""),
            backend=str(body.get("backend") or "claude"),
            resume_session_id=str(body.get("sessionId") or body.get("resumeSessionId") or "").strip() or None,
            max_turns=int(body.get("maxTurns") or agent.DEFAULT_MAX_TURNS),
        )
        data["status"] = build_status()
        return data
    except Exception as e:
        return {"ok": False, "error": str(e), "status": build_status()}


def do_agent_poll(body: dict) -> dict:
    try:
        data = agent.poll_task(
            str(body.get("taskId") or ""),
            backend=str(body.get("backend") or "claude"),
            since=int(body.get("since") or 0),
            game_id=str(body.get("gameId") or "").strip() or None,
        )
        # 轮询高频：不要附带完整 build_status（体积大且会拖垮连接）
        data.pop("events", None)
        data["ok"] = True
        return data
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_agent_cancel(body: dict) -> dict:
    try:
        data = agent.cancel_task(
            str(body.get("taskId") or ""),
            backend=str(body.get("backend") or "claude"),
        )
        data["status"] = build_status()
        return data
    except Exception as e:
        return {"ok": False, "error": str(e), "status": build_status()}


def do_ping_editor() -> dict:
    status = build_status()
    if not status["loggedIn"]:
        return {"ok": False, "error": "尚未登录", "status": status}
    cid = int(status["characterId"] or 0)
    if not cid:
        return {"ok": False, "error": "请先填写 character_id（卡 ID）", "status": status}
    try:
        cookie, token, remain, email = studio.load_auth()
        info, game_id = studio.ensure_editor(cookie, token, cid)
        return {
            "ok": True,
            "email": email,
            "remainSec": remain,
            "gameId": game_id,
            "editorStatus": info.get("status"),
            "containerId": info.get("containerId"),
            "workbenchUrl": f"{studio.get_origin()}/studio/game-creation/workbench?character_id={cid}",
            "status": build_status(),
        }
    except SystemExit as e:
        return _ping_fail(str(e), status, cid)
    except Exception as e:
        return _ping_fail(str(e), status, cid)


_CAPTCHA_OPEN_AT = 0.0


def _extract_captcha_path(raw: str) -> str:
    text = str(raw or "")
    # 典型: ... captcha_required ... "location":"/__captcha/" ...
    m = re.search(r'"location"\s*:\s*"([^"]+)"', text)
    if m:
        loc = m.group(1).strip() or "/__captcha/"
        return loc if loc.startswith("/") else f"/{loc}"
    if "captcha" in text.lower() or "/__captcha/" in text.lower() or "418" in text:
        return "/__captcha/"
    return ""


def _open_captcha_browser(path: str = "/__captcha/") -> str:
    """主动弹出系统浏览器打开验证码页；返回完整 URL。短时内去抖。"""
    global _CAPTCHA_OPEN_AT
    origin = str(studio.get_origin() or "").rstrip("/")
    loc = path if path.startswith("/") else f"/{path or '__captcha__'}"
    url = f"{origin}{loc}"
    now = time.time()
    if now - _CAPTCHA_OPEN_AT >= 6.0:
        _CAPTCHA_OPEN_AT = now

        def _open():
            try:
                webbrowser.open(url)
            except Exception as err:
                print(f"[console] 打开验证码页失败: {err}")

        threading.Thread(target=_open, name="open-captcha", daemon=True).start()
        print(f"[console] 已弹出浏览器验证: {url}")
    return url


def _ping_fail(raw: str, status: dict, cid: int = 0) -> dict:
    captcha_path = _extract_captcha_path(raw)
    captcha_url = ""
    if captcha_path:
        captcha_url = _open_captcha_browser(captcha_path)
    workbench = ""
    if cid:
        workbench = f"{studio.get_origin()}/studio/game-creation/workbench?character_id={cid}"
    out = {
        "ok": False,
        "error": _friendly_ping_error(raw, bool(captcha_url)),
        "status": status,
    }
    if captcha_url:
        out["captchaRequired"] = True
        out["captchaUrl"] = captcha_url
    if workbench:
        out["workbenchUrl"] = workbench
    return out


def _friendly_ping_error(raw: str, opened: bool = False) -> str:
    text = str(raw or "")
    low = text.lower()
    if "captcha_required" in low or "/__captcha/" in low or "418" in low:
        if opened:
            return (
                "连接编辑器需要过站点验证码，已自动打开浏览器。"
                "请在弹出页完成验证后，回到控制台再点一次「连接编辑器」。"
            )
        return (
            "连接编辑器需要过站点验证码。"
            "请用浏览器打开当前 origin 完成验证后，"
            "再在控制台更新 cookie / 重新登录，然后点「连接编辑器」。"
        )
    return text or "连接/创建编辑器失败"


def pull_job_snapshot() -> dict:
    with _PULL_LOCK:
        logs = list(_PULL_JOB.get("logs") or [])[-40:]
        failed = list(_PULL_JOB.get("failedPaths") or [])
        result = _PULL_JOB.get("result")
        out = _PULL_JOB.get("out") or ""
        if result and isinstance(result, dict) and result.get("failedPaths"):
            failed = list(result.get("failedPaths") or failed)
        if (not failed) and out and (not _PULL_JOB.get("running")):
            try:
                meta = puller.read_pull_meta(Path(out))
                failed = list(meta.get("failed_paths") or [])
                if failed and not _PULL_JOB.get("fail"):
                    _PULL_JOB["fail"] = len(failed)
            except Exception:
                pass
        return {
            "running": bool(_PULL_JOB.get("running")),
            "phase": _PULL_JOB.get("phase") or "",
            "message": _PULL_JOB.get("message") or "",
            "current": int(_PULL_JOB.get("current") or 0),
            "total": int(_PULL_JOB.get("total") or 0),
            "okCount": int(_PULL_JOB.get("ok") or 0),
            "failCount": int(_PULL_JOB.get("fail") or len(failed) or 0),
            "failedPaths": failed,
            "canRetryFailed": (not _PULL_JOB.get("running")) and len(failed) > 0,
            "out": out,
            "error": _PULL_JOB.get("error") or "",
            "done": bool(_PULL_JOB.get("done")),
            "result": _PULL_JOB.get("result"),
            "mode": _PULL_JOB.get("mode") or "",
            "logs": logs,
        }


def _pull_progress(payload: dict) -> None:
    with _PULL_LOCK:
        for key in ("phase", "message", "current", "total", "ok", "fail", "out"):
            if key in payload and payload[key] is not None:
                _PULL_JOB[key] = payload[key]
        msg = payload.get("message")
        if msg:
            logs = _PULL_JOB.setdefault("logs", [])
            logs.append(str(msg))
            if len(logs) > 200:
                del logs[:-120]


def _finish_pull_summary(summary: dict) -> None:
    failed = list(summary.get("failedPaths") or [])
    with _PULL_LOCK:
        _PULL_JOB["running"] = False
        _PULL_JOB["done"] = True
        _PULL_JOB["result"] = summary
        _PULL_JOB["out"] = summary.get("out") or ""
        _PULL_JOB["message"] = summary.get("message") or "拉取完成"
        _PULL_JOB["fail"] = int(summary.get("failed") or 0)
        _PULL_JOB["ok"] = int(summary.get("pulled") or 0)
        _PULL_JOB["failedPaths"] = failed
        _PULL_JOB["mode"] = summary.get("mode") or _PULL_JOB.get("mode") or ""
        if failed:
            _PULL_JOB["error"] = f"有 {len(failed)} 个文件失败"
        else:
            _PULL_JOB["error"] = ""


def _fail_pull_job(prefix: str, err: BaseException) -> None:
    with _PULL_LOCK:
        _PULL_JOB["running"] = False
        _PULL_JOB["done"] = True
        _PULL_JOB["error"] = str(err)
        _PULL_JOB["message"] = f"{prefix}：{err}"


def _run_pull_job(character_id: int, out: str | None) -> None:
    try:
        out_path = Path(out).expanduser() if out else None
        summary = puller.pull_project(
            character_id,
            out_path,
            on_progress=_pull_progress,
        )
        _finish_pull_summary(summary)
    except SystemExit as e:
        _fail_pull_job("拉取失败", e)
    except Exception as e:
        _fail_pull_job("拉取失败", e)


def _run_retry_failed_job(character_id: int, out: str | None, failed_paths: list[str] | None) -> None:
    try:
        out_path = Path(out).expanduser() if out else None
        summary = puller.retry_failed(
            character_id,
            out_path,
            on_progress=_pull_progress,
            failed_paths=failed_paths,
        )
        _finish_pull_summary(summary)
    except SystemExit as e:
        _fail_pull_job("重试失败", e)
    except Exception as e:
        _fail_pull_job("重试失败", e)


def _resolve_pull_target(body: dict, status: dict) -> tuple[int, str]:
    cid = body.get("characterId")
    if cid is None or str(cid).strip() == "":
        cid = status["characterId"]
    try:
        cid = int(cid or 0)
    except Exception:
        cid = 0
    out = str(body.get("projectPath") or body.get("out") or "").strip()
    if not out:
        # 走 default_out_dir：换卡时不会误用旧卡目录
        out = str(puller.default_out_dir(cid)) if cid else ""
    elif cid:
        path = Path(out).expanduser().resolve()
        meta_cid = int(puller.read_pull_meta(path).get("character_id") or 0)
        if meta_cid and meta_cid != cid:
            out = str(puller.default_out_dir(cid))
    return cid, out


def do_start_pull(body: dict) -> dict:
    status = build_status()
    if not status["loggedIn"]:
        return {"ok": False, "error": "尚未登录", "status": status}

    cid, out = _resolve_pull_target(body, status)
    if not cid:
        return {"ok": False, "error": "请先填写 character_id", "status": status}

    studio.save_config({
        "character_id": cid,
        "project_path": out or str(puller.default_out_dir(cid)),
    })

    claim = _claim_project_job("pull", {
        "running": True,
        "phase": "start",
        "message": "开始拉取容器…",
        "current": 0,
        "total": 0,
        "ok": 0,
        "fail": 0,
        "failedPaths": [],
        "mode": "full",
        "out": out or str(puller.default_out_dir(cid)),
        "error": "",
        "done": False,
        "result": None,
        "logs": ["开始拉取容器完整项目…"],
    })
    if claim == "sync":
        return {"ok": False, "error": "同步进行中，请稍后再拉取", "job": pull_job_snapshot(), "status": status}
    if claim:
        return {"ok": False, "error": "已有拉取任务进行中", "job": pull_job_snapshot(), "status": status}

    thread = threading.Thread(
        target=_run_pull_job,
        args=(cid, out or None),
        daemon=True,
    )
    thread.start()
    return {
        "ok": True,
        "message": "拉取已开始",
        "job": pull_job_snapshot(),
        "status": build_status(),
    }


def do_retry_failed_pull(body: dict) -> dict:
    status = build_status()
    if not status["loggedIn"]:
        return {"ok": False, "error": "尚未登录", "status": status}

    cid, out = _resolve_pull_target(body, status)
    if not cid:
        return {"ok": False, "error": "请先填写 character_id", "status": status}

    failed_paths = body.get("failedPaths")
    if not isinstance(failed_paths, list):
        failed_paths = None
    if not failed_paths:
        with _PULL_LOCK:
            result = _PULL_JOB.get("result") or {}
            failed_paths = list(_PULL_JOB.get("failedPaths") or [])
            if not failed_paths and isinstance(result, dict):
                failed_paths = list(result.get("failedPaths") or [])
    if not failed_paths:
        # 再读磁盘 meta
        out_dir = Path(out) if out else puller.default_out_dir(cid)
        meta = puller.read_pull_meta(out_dir)
        failed_paths = list(meta.get("failed_paths") or [])
    if not failed_paths:
        return {"ok": False, "error": "没有失败文件可重试", "job": pull_job_snapshot(), "status": status}

    studio.save_config({"character_id": cid, "project_path": out})

    claim = _claim_project_job("pull", {
        "running": True,
        "phase": "start",
        "message": f"重试失败文件 {len(failed_paths)} 个…",
        "current": 0,
        "total": len(failed_paths),
        "ok": 0,
        "fail": 0,
        "failedPaths": list(failed_paths),
        "mode": "retry",
        "out": out,
        "error": "",
        "done": False,
        "result": None,
        "logs": [f"只重拉失败文件（{len(failed_paths)}）…"],
    })
    if claim == "sync":
        return {"ok": False, "error": "同步进行中，请稍后再重试拉取", "job": pull_job_snapshot(), "status": status}
    if claim:
        return {"ok": False, "error": "已有拉取任务进行中", "job": pull_job_snapshot(), "status": status}

    thread = threading.Thread(
        target=_run_retry_failed_job,
        args=(cid, out or None, list(failed_paths)),
        daemon=True,
    )
    thread.start()
    return {
        "ok": True,
        "message": "已开始重试失败文件",
        "job": pull_job_snapshot(),
        "status": build_status(),
    }


def _port_in_use(port: int) -> bool:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        try:
            sock.close()
        except Exception:
            pass


def cloud_mirror_root(character_id: int) -> Path:
    return (KIT / ".cloud-preview" / str(int(character_id))).resolve()


def cloud_mirror_snapshot() -> dict:
    with _CLOUD_MIRROR_LOCK:
        return {
            "enabled": bool(_CLOUD_MIRROR.get("enabled")),
            "running": bool(_CLOUD_MIRROR.get("running")),
            "characterId": int(_CLOUD_MIRROR.get("characterId") or 0),
            "lastAt": float(_CLOUD_MIRROR.get("lastAt") or 0),
            "changedAt": float(_CLOUD_MIRROR.get("changedAt") or 0),
            "revision": int(_CLOUD_MIRROR.get("revision") or 0),
            "message": str(_CLOUD_MIRROR.get("message") or ""),
            "error": str(_CLOUD_MIRROR.get("error") or ""),
            "downloaded": int(_CLOUD_MIRROR.get("downloaded") or 0),
            "skipped": int(_CLOUD_MIRROR.get("skipped") or 0),
            "intervalSec": float(_CLOUD_MIRROR.get("intervalSec") or 6),
            "mirrorRoot": str(cloud_mirror_root(int(_CLOUD_MIRROR.get("characterId") or 0)))
            if int(_CLOUD_MIRROR.get("characterId") or 0)
            else "",
        }


def preview_source_get() -> str:
    global _PREVIEW_SOURCE
    return "cloud" if _PREVIEW_SOURCE == "cloud" else "local"


def _stop_cloud_mirror() -> None:
    global _CLOUD_MIRROR_THREAD
    with _CLOUD_MIRROR_LOCK:
        _CLOUD_MIRROR["enabled"] = False
        _CLOUD_MIRROR["stop"] = True
        _CLOUD_MIRROR["message"] = "已停止云端镜像"
    th = _CLOUD_MIRROR_THREAD
    if th and th.is_alive() and th is not threading.current_thread():
        th.join(timeout=1.5)
    with _CLOUD_MIRROR_LOCK:
        if not (_CLOUD_MIRROR_THREAD and _CLOUD_MIRROR_THREAD.is_alive()):
            _CLOUD_MIRROR_THREAD = None
            _CLOUD_MIRROR["running"] = False


def _cloud_mirror_has_index(character_id: int) -> bool:
    return (cloud_mirror_root(int(character_id or 0)) / "publish" / "index.html").is_file()


def _cloud_mirror_loop(gen: int) -> None:
    while True:
        with _CLOUD_MIRROR_LOCK:
            if gen != _CLOUD_MIRROR_GEN or _CLOUD_MIRROR.get("stop") or not _CLOUD_MIRROR.get("enabled"):
                if gen == _CLOUD_MIRROR_GEN:
                    _CLOUD_MIRROR["running"] = False
                    _CLOUD_MIRROR["enabled"] = False
                break
            cid = int(_CLOUD_MIRROR.get("characterId") or 0)
            interval = max(3.0, float(_CLOUD_MIRROR.get("intervalSec") or 6))
            force = bool(_CLOUD_MIRROR.pop("forceNext", False))
        if cid <= 0:
            time.sleep(1.0)
            continue
        if _project_job_busy():
            with _CLOUD_MIRROR_LOCK:
                if gen == _CLOUD_MIRROR_GEN:
                    _CLOUD_MIRROR["message"] = "同步/拉取进行中，云端镜像暂缓…"
            _sleep_interruptible(interval)
            continue
        try:
            with _CLOUD_MIRROR_LOCK:
                if gen != _CLOUD_MIRROR_GEN:
                    break
                _CLOUD_MIRROR["running"] = True
                _CLOUD_MIRROR["message"] = "正在拉取云端 publish…"
                _CLOUD_MIRROR["error"] = ""
            result = puller.mirror_remote_publish(
                cid,
                cloud_mirror_root(cid),
                force=force,
                should_stop=lambda: gen != _CLOUD_MIRROR_GEN,
            )
            with _CLOUD_MIRROR_LOCK:
                if gen != _CLOUD_MIRROR_GEN:
                    break
                _CLOUD_MIRROR["running"] = False
                _CLOUD_MIRROR["lastAt"] = time.time()
                _CLOUD_MIRROR["message"] = str(result.get("message") or "云端已同步")
                _CLOUD_MIRROR["downloaded"] = int(result.get("downloaded") or 0)
                _CLOUD_MIRROR["skipped"] = int(result.get("skipped") or 0)
                _CLOUD_MIRROR["error"] = ""
                if result.get("changed"):
                    _CLOUD_MIRROR["changedAt"] = time.time()
                    _CLOUD_MIRROR["revision"] = int(_CLOUD_MIRROR.get("revision") or 0) + 1
        except Exception as e:
            with _CLOUD_MIRROR_LOCK:
                if gen != _CLOUD_MIRROR_GEN:
                    break
                _CLOUD_MIRROR["running"] = False
                _CLOUD_MIRROR["error"] = str(e)
                _CLOUD_MIRROR["message"] = f"云端拉取失败：{e}"
                _CLOUD_MIRROR["lastAt"] = time.time()
        _sleep_interruptible(interval)


def _sleep_interruptible(seconds: float) -> None:
    end = time.time() + max(0.2, float(seconds))
    while time.time() < end:
        with _CLOUD_MIRROR_LOCK:
            if _CLOUD_MIRROR.get("stop") or not _CLOUD_MIRROR.get("enabled"):
                return
        time.sleep(0.25)


def _await_and_start_cloud_preview(character_id: int, status: dict) -> None:
    """后台等镜像出现 index.html 后自动启动云端预览。"""
    cid = int(character_id or 0)
    deadline = time.time() + 600
    while time.time() < deadline:
        if preview_source_get() != "cloud":
            return
        with _CLOUD_MIRROR_LOCK:
            if int(_CLOUD_MIRROR.get("characterId") or 0) != cid:
                return
            err = str(_CLOUD_MIRROR.get("error") or "")
            running = bool(_CLOUD_MIRROR.get("running"))
            enabled = bool(_CLOUD_MIRROR.get("enabled"))
        if not enabled:
            return
        if _cloud_mirror_has_index(cid):
            snap = preview_snapshot()
            if snap.get("running") and snap.get("source") == "cloud":
                return
            do_start_preview({
                "characterId": cid,
                "projectPath": status.get("projectPath") or "",
                "previewPort": status.get("previewPort") or 8791,
                "source": "cloud",
            })
            return
        if err and not running:
            # 留给轮询展示错误；短暂等待以便重试循环再跑
            time.sleep(2.0)
        else:
            time.sleep(0.8)


def _start_cloud_mirror(character_id: int, *, force_first: bool | None = None) -> dict:
    """启动后台镜像线程（不阻塞请求）。有本地缓存时增量；无 index 时强制首拉。"""
    global _CLOUD_MIRROR_THREAD, _CLOUD_MIRROR_GEN
    cid = int(character_id or 0)
    if cid <= 0:
        raise RuntimeError("缺少 character_id")
    _stop_cloud_mirror()
    has_index = _cloud_mirror_has_index(cid)
    if force_first is None:
        force_first = not has_index
    with _CLOUD_MIRROR_LOCK:
        _CLOUD_MIRROR_GEN += 1
        gen = _CLOUD_MIRROR_GEN
        _CLOUD_MIRROR.update({
            "enabled": True,
            "stop": False,
            "running": False,
            "characterId": cid,
            "forceNext": bool(force_first),
            "message": (
                "正在拉取云端 publish…"
                if force_first or not has_index
                else "云端镜像后台增量同步中…"
            ),
            "error": "",
            "downloaded": 0,
            "skipped": 0,
        })
        _CLOUD_MIRROR_THREAD = threading.Thread(
            target=_cloud_mirror_loop,
            args=(gen,),
            name="dzmm-cloud-mirror",
            daemon=True,
        )
        _CLOUD_MIRROR_THREAD.start()
    return {
        "ok": True,
        "pending": not has_index,
        "hasIndex": has_index,
        "forceFirst": bool(force_first),
        "mirrorRoot": str(cloud_mirror_root(cid)),
        "message": "已启动云端镜像线程",
    }


def do_preview_source_get() -> dict:
    cid = int(cloud_mirror_snapshot().get("characterId") or 0)
    if not cid:
        try:
            cid = int((build_status() or {}).get("characterId") or 0)
        except Exception:
            cid = 0
    desired = preview_source_get()
    pending = desired == "cloud" and (not cid or not _cloud_mirror_has_index(cid))
    return {
        "ok": True,
        "source": desired,
        "desiredSource": desired,
        "preview": preview_snapshot(),
        "cloud": cloud_mirror_snapshot(),
        "pending": pending,
        "status": build_status(),
    }


def do_preview_source_set(body: dict) -> dict:
    """切换预览源：local=本机工程；cloud=持续镜像容器 /publish 到旁路目录再预览。"""
    global _PREVIEW_SOURCE
    mode = str((body or {}).get("source") or (body or {}).get("mode") or "local").strip().lower()
    if mode not in ("local", "cloud"):
        return {"ok": False, "error": "source 仅支持 local / cloud", "status": build_status()}

    status = build_status()
    if not status.get("loggedIn"):
        return {"ok": False, "error": "尚未登录", "status": status}
    cid = int(status.get("characterId") or 0)
    if body.get("characterId") not in (None, ""):
        try:
            cid = int(body.get("characterId") or cid)
        except Exception:
            pass
    if not cid:
        return {"ok": False, "error": "请先填写 character_id", "status": status}

    was_running = bool(preview_snapshot().get("running"))
    try:
        if mode == "cloud":
            first = _start_cloud_mirror(cid)
            _PREVIEW_SOURCE = "cloud"
            if first.get("hasIndex"):
                result = do_start_preview({
                    "characterId": cid,
                    "projectPath": status.get("projectPath") or "",
                    "previewPort": status.get("previewPort") or 8791,
                    "source": "cloud",
                })
                if not result.get("ok"):
                    _stop_cloud_mirror()
                    _PREVIEW_SOURCE = "local"
                    return result
                result["source"] = "cloud"
                result["desiredSource"] = "cloud"
                result["cloud"] = cloud_mirror_snapshot()
                result["mirror"] = first
                result["pending"] = False
                result["message"] = "已切换到云端预览（持续拉取容器 publish，不覆盖本地工程）"
                return result

            # 首次拉取：先停本地预览，避免界面误显示「已是云端」
            do_stop_preview()
            threading.Thread(
                target=_await_and_start_cloud_preview,
                args=(cid, status),
                name="dzmm-cloud-preview-boot",
                daemon=True,
            ).start()
            return {
                "ok": True,
                "pending": True,
                "source": "cloud",
                "desiredSource": "cloud",
                "preview": preview_snapshot(),
                "cloud": cloud_mirror_snapshot(),
                "mirror": first,
                "status": build_status(),
                "message": "正在首次拉取云端 publish，完成后自动打开预览（不覆盖本地工程）…",
            }

        _stop_cloud_mirror()
        _PREVIEW_SOURCE = "local"
        if was_running:
            result = do_start_preview({
                "characterId": cid,
                "projectPath": status.get("projectPath") or "",
                "previewPort": status.get("previewPort") or 8791,
                "source": "local",
            })
        else:
            result = {"ok": True, "preview": preview_snapshot(), "status": build_status()}
        result["source"] = "local"
        result["cloud"] = cloud_mirror_snapshot()
        result["pending"] = False
        result["message"] = "已切换到本地预览"
        return result
    except Exception as e:
        _stop_cloud_mirror()
        _PREVIEW_SOURCE = "local"
        return {"ok": False, "error": str(e), "source": "local", "status": build_status()}


def preview_snapshot() -> dict:
    global _PREVIEW_PROC
    with _PREVIEW_LOCK:
        running = False
        if _PREVIEW_PROC is not None:
            code = _PREVIEW_PROC.poll()
            if code is None:
                running = True
            else:
                if not _PREVIEW_META.get("error"):
                    _PREVIEW_META["error"] = f"预览进程已退出 code={code}"
                    _PREVIEW_META["message"] = _PREVIEW_META["error"]
                _PREVIEW_PROC = None
                _PREVIEW_META["pid"] = 0
        port = int(_PREVIEW_META.get("port") or 8791)
        url = _PREVIEW_META.get("url") or f"http://127.0.0.1:{port}/"
        # 进程句柄丢失时，用端口探测兜底（避免 UI 显示未启动但服务已在听）
        if not running and _port_in_use(port):
            running = True
            if not _PREVIEW_META.get("message"):
                _PREVIEW_META["message"] = f"预览运行中 {url}"
        _PREVIEW_META["running"] = running
        _PREVIEW_META["url"] = url if running or _PREVIEW_META.get("url") else ""
        # meta.source = 实际在播的目录；顶层 desiredSource = 用户选择的本地/云端
        snap = dict(_PREVIEW_META)
        snap["source"] = str(_PREVIEW_META.get("source") or "local")
        snap["desiredSource"] = preview_source_get()
        snap["cloud"] = cloud_mirror_snapshot()
        return snap


def _kill_port_listeners(port: int) -> None:
    """Best-effort: stop whatever is listening on preview port (Windows)."""
    try:
        import subprocess as sp

        out = sp.check_output(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue)."
             "OwningProcess | Select-Object -Unique"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        for line in out.splitlines():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            if pid <= 0:
                continue
            try:
                sp.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=8)
            except Exception:
                pass
    except Exception:
        pass


def _stop_preview_locked() -> None:
    global _PREVIEW_PROC
    proc = _PREVIEW_PROC
    _PREVIEW_PROC = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception:
            pass
    port = int(_PREVIEW_META.get("port") or 8791)
    if _port_in_use(port):
        _kill_port_listeners(port)
    _PREVIEW_META.update({
        "running": False,
        "pid": 0,
        "url": "",
        "message": "预览已停止",
        "error": "",
        "source": "local",
        "publishDir": "",
    })


def do_stop_preview() -> dict:
    with _PREVIEW_LOCK:
        _stop_preview_locked()
    return {"ok": True, "preview": preview_snapshot(), "status": build_status()}


def _preview_http_json(path: str, method: str = "GET", body: dict | None = None, timeout: float = 20):
    snap = preview_snapshot()
    if not snap.get("running") or not snap.get("url"):
        raise RuntimeError("预览未启动")
    base = str(snap["url"]).rstrip("/")
    url = base + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8", "replace") or "{}")


def do_bridge_status(force: bool = False) -> dict:
    preview = preview_snapshot()
    if not preview.get("running"):
        return {
            "ok": False,
            "error": "预览未启动",
            "preview": preview,
            "bridge": None,
        }
    try:
        path = "/health?force=1" if force else "/health"
        health = _preview_http_json(path)
        return {"ok": True, "preview": preview, "bridge": health}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "preview": preview,
            "bridge": None,
        }


def do_bridge_auth_reload() -> dict:
    """登录切换后强制预览进程重读 cookie / 昵称。"""
    preview = preview_snapshot()
    if not preview.get("running"):
        return {"ok": False, "error": "预览未启动", "preview": preview}
    try:
        data = _preview_http_json("/_dzmm/auth/reload", method="POST", body={})
        health = _preview_http_json("/health?force=1")
        return {"ok": True, "reload": data, "preview": preview, "bridge": health}
    except Exception as e:
        # 旧预览进程可能还没有 /_dzmm/auth/reload，退化为 force health
        try:
            health = _preview_http_json("/health?force=1")
            return {"ok": True, "reload": None, "preview": preview, "bridge": health, "warn": str(e)}
        except Exception as e2:
            return {"ok": False, "error": str(e2), "preview": preview}


def do_bridge_publish(body: dict) -> dict:
    preview = preview_snapshot()
    status = build_status()
    cfg_cid = int(status.get("characterId") or 0)
    if not preview.get("running"):
        # 预览未开时，直接走 studio publish
        if not status["loggedIn"]:
            return {"ok": False, "error": "尚未登录"}
        if not cfg_cid:
            return {"ok": False, "error": "缺少 character_id"}
        try:
            cookie, token, remain, email = studio.load_auth()
            studio.ensure_editor(cookie, token, cfg_cid)
            studio.publish(cookie, token, cfg_cid)
            return {
                "ok": True,
                "message": "已发布到线上玩家版",
                "via": "studio",
                "remainSec": remain,
                "email": email,
            }
        except SystemExit as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    prev_cid = int(preview.get("characterId") or 0)
    if prev_cid and cfg_cid and prev_cid != cfg_cid:
        return {
            "ok": False,
            "error": (
                f"预览仍绑定旧卡 {prev_cid}，当前配置为 {cfg_cid}。"
                "请先停止并重新启动预览后再发布。"
            ),
            "preview": preview,
        }

    try:
        health = _preview_http_json("/health")
        live_cid = int((health or {}).get("characterId") or 0)
        if live_cid and cfg_cid and live_cid != cfg_cid:
            return {
                "ok": False,
                "error": (
                    f"预览进程实际绑定卡 {live_cid}，当前配置为 {cfg_cid}。"
                    "请停止预览并重新启动后再发布。"
                ),
                "preview": preview,
                "bridge": health,
            }
        job = _preview_http_json(
            "/_dzmm/studio/publish",
            method="POST",
            body={"message": str((body or {}).get("message") or "publish from local console")},
            timeout=30,
        )
        return {"ok": True, "job": job, "via": "preview"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_bridge_publish_status() -> dict:
    preview = preview_snapshot()
    if not preview.get("running"):
        return {"ok": True, "job": {"status": "idle"}, "preview": preview}
    try:
        job = _preview_http_json("/_dzmm/studio/publish", method="GET", timeout=15)
        return {"ok": True, "job": job, "preview": preview}
    except Exception as e:
        return {"ok": False, "error": str(e), "preview": preview}


def sync_job_snapshot() -> dict:
    with _SYNC_LOCK:
        return {
            "running": bool(_SYNC_JOB.get("running")),
            "phase": _SYNC_JOB.get("phase") or "",
            "message": _SYNC_JOB.get("message") or "",
            "current": int(_SYNC_JOB.get("current") or 0),
            "total": int(_SYNC_JOB.get("total") or 0),
            "okCount": int(_SYNC_JOB.get("ok") or 0),
            "failCount": int(_SYNC_JOB.get("fail") or 0),
            "error": _SYNC_JOB.get("error") or "",
            "done": bool(_SYNC_JOB.get("done")),
            "fails": list(_SYNC_JOB.get("fails") or []),
            "gameId": _SYNC_JOB.get("gameId") or "",
        }


def _sync_set(**kwargs) -> None:
    with _SYNC_LOCK:
        _SYNC_JOB.update(kwargs)


def _run_sync_job(cid: int, message: str, only, full: bool = False) -> None:
    try:
        _sync_set(phase="scan", message="扫描变更…", current=0, total=0, ok=0, fail=0, error="", fails=[])
        studio.refresh_root()
        cookie, token, remain, email = studio.load_auth()
        _, game_id = studio.ensure_editor(cookie, token, cid)
        _sync_set(gameId=str(game_id or ""), phase="auth", message="连接编辑器…")

        all_local = studio.list_local_files()
        partial = isinstance(only, list) and bool(only)
        files = all_local
        if partial:
            only_set = {str(p).replace("\\", "/") for p in only}
            files = [f for f in files if f.relative_to(studio.ROOT).as_posix() in only_set]
        if not files:
            _sync_set(
                running=False,
                done=True,
                phase="error",
                error="没有可同步的文件（检查本地项目路径是否含 publish/）",
                message="同步失败",
            )
            return

        to_upload, meta, mode = studio.select_files_to_sync(
            files, full=bool(full), character_id=int(cid)
        )
        if mode == "baseline":
            _sync_set(
                running=False,
                done=True,
                phase="done",
                message=f"已建立增量基线（{len(meta.get('files') or {})} 个），无文件需上传",
                ok=0,
                fail=0,
                error="",
                current=0,
                total=0,
            )
            return

        ok = fail = 0
        fails: list[str] = []
        deleted_n = 0
        git_pending = bool(meta.get("gitSavePending"))

        if to_upload:
            total = len(to_upload)
            upload_label = "全量上传" if mode in ("full", "retarget") else "增量上传"
            if mode == "retarget":
                upload_label = "换卡全量上传"
            _sync_set(
                phase="upload",
                message=f"{upload_label} 0/{total}（共扫描 {len(files)}）",
                current=0,
                total=total,
            )
            # 全量/换卡只记录本轮成功签名，避免失败项沿用旧哈希后被永久跳过
            entries = {} if mode in ("full", "retarget") else dict(meta.get("files") or {})
            for i, path in enumerate(to_upload, start=1):
                rel = path.relative_to(studio.ROOT).as_posix()
                try:
                    studio.upload_file(cookie, token, game_id, path)
                    entries[rel] = studio._file_sig(path)
                    ok += 1
                except Exception as e:
                    fail += 1
                    entries.pop(rel, None)
                    if len(fails) < 8:
                        fails.append(f"{rel}: {e}")
                _sync_set(
                    current=i,
                    ok=ok,
                    fail=fail,
                    fails=list(fails),
                    message=f"{upload_label} {i}/{total}" + (f"（失败 {fail}）" if fail else ""),
                )
            # drop signatures for local files that no longer exist
            keep_rels = {p.relative_to(studio.ROOT).as_posix() for p in all_local}
            entries = {k: v for k, v in entries.items() if k in keep_rels}
            meta["files"] = entries
            meta["character_id"] = cid
            studio.save_sync_meta(meta)
            if fail:
                _sync_set(
                    running=False,
                    done=True,
                    phase="error",
                    error=f"部分失败：成功 {ok}，失败 {fail}（失败文件下次会重试）",
                    message=f"同步结束：成功 {ok}，失败 {fail}",
                    ok=ok,
                    fail=fail,
                    fails=fails,
                )
                return
        else:
            # prune stale meta keys even when nothing to upload
            keep_rels = {p.relative_to(studio.ROOT).as_posix() for p in all_local}
            prev = dict(meta.get("files") or {})
            cleaned = {k: v for k, v in prev.items() if k in keep_rels}
            if cleaned != prev:
                meta["files"] = cleaned
                meta["character_id"] = cid
                studio.save_sync_meta(meta)

        # 整包同步才对照本地删容器多余文件；指定 only 时不删，避免误伤
        prune_err = ""
        if not partial:
            _sync_set(phase="prune", message="清理容器多余文件…", current=0, total=0)
            prune = None
            last_prune_err: Exception | None = None
            for attempt in range(1, 4):
                try:
                    prune = studio.prune_remote_extras(cookie, token, game_id, all_local)
                    last_prune_err = None
                    break
                except Exception as e:
                    last_prune_err = e
                    _sync_set(
                        phase="prune",
                        message=f"清理连接中断，正在重试（{attempt}/3）…",
                    )
                    time.sleep(0.8 * attempt)
            if prune is None:
                prune_err = str(last_prune_err or "清理失败")
                prune = {"ok": 0, "fails": [prune_err], "deleted": []}
                fails.append(f"清理失败：{prune_err}")
            deleted_n = int(prune.get("ok") or 0)
            prune_fails = list(prune.get("fails") or [])
            if prune_fails:
                fails.extend(prune_fails[: max(0, 8 - len(fails))])
            if prune_err:
                # 上传已成功：不因列目录 SSL 掐断而跳过 git save
                _sync_set(
                    phase="prune",
                    message="清理因线路中断未完成，先保存已上传文件…",
                    fails=list(fails),
                )
            elif prune_fails and deleted_n == 0 and not to_upload and not git_pending:
                _sync_set(
                    running=False,
                    done=True,
                    phase="error",
                    error=f"清理多余文件失败：{prune_fails[0]}",
                    message="同步失败：清理多余文件出错",
                    ok=ok,
                    fail=len(prune_fails),
                    fails=fails,
                )
                return
            elif deleted_n:
                _sync_set(
                    phase="prune",
                    message=f"已删除多余 {deleted_n} 个" + (f"（失败 {len(prune_fails)}）" if prune_fails else ""),
                    ok=ok,
                    fail=fail + len(prune_fails),
                    fails=list(fails),
                )

        need_git = bool(to_upload or deleted_n or git_pending)
        if not need_git:
            _sync_set(
                running=False,
                done=True,
                phase="done",
                message="无变更文件，已跳过",
                ok=0,
                fail=0,
                error="",
                current=0,
                total=0,
            )
            return

        done_label = {
            "retarget": "换卡全量同步",
            "full": "全量同步",
        }.get(mode, "增量同步")
        if not to_upload and deleted_n and not git_pending:
            done_label = "清理多余文件"
        elif not to_upload and git_pending:
            done_label = "补做 git save"
        _sync_set(phase="git", message="保存容器 git…")
        try:
            studio.git_save(cookie, token, game_id, message)
            meta["gitSavePending"] = False
            meta.pop("gitSavePending", None)
            meta["character_id"] = cid
            studio.save_sync_meta(meta)
        except Exception as e:
            meta["gitSavePending"] = True
            meta["character_id"] = cid
            studio.save_sync_meta(meta)
            _sync_set(
                running=False,
                done=True,
                phase="error",
                error=f"文件已上传，但 git save 失败：{e}（下次同步会重试）",
                message="git save 失败，已标记待重试",
                ok=ok,
                fail=1,
                fails=[str(e)],
            )
            return
        msg = f"已{done_label} {ok} 个文件并保存"
        if deleted_n:
            msg = f"已{done_label} 上传 {ok}、删除多余 {deleted_n} 并保存"
        if prune_err:
            msg += "；清理因线路中断未完成，下次同步会再清"
        _sync_set(
            running=False,
            done=True,
            phase="done",
            message=msg,
            ok=ok,
            fail=0 if not prune_err else 1,
            error=prune_err,
        )
    except SystemExit as e:
        _sync_set(running=False, done=True, phase="error", error=str(e), message=f"同步失败：{e}")
    except Exception as e:
        _sync_set(running=False, done=True, phase="error", error=str(e), message=f"同步失败：{e}")


def do_start_sync(body: dict) -> dict:
    """Start background incremental sync to Workbench + git save."""
    status = build_status()
    if not status.get("loggedIn"):
        return {"ok": False, "error": "尚未登录", "job": sync_job_snapshot()}

    cid = body.get("characterId")
    if cid is None or str(cid).strip() == "":
        cid = status.get("characterId")
    try:
        cid = int(cid or 0)
    except Exception:
        cid = 0
    if not cid:
        return {"ok": False, "error": "缺少 character_id", "job": sync_job_snapshot()}

    project = str(body.get("projectPath") or status.get("projectPath") or "").strip()
    if project:
        studio.save_config({"character_id": cid, "project_path": project})
    else:
        studio.save_config({"character_id": cid})

    message = str((body or {}).get("message") or "sync from local console").strip() or "sync from local console"
    only = (body or {}).get("only")
    full = bool((body or {}).get("full"))

    claim = _claim_project_job("sync", {
        "running": True,
        "phase": "start",
        "message": "正在启动同步…",
        "current": 0,
        "total": 0,
        "ok": 0,
        "fail": 0,
        "error": "",
        "done": False,
        "fails": [],
        "gameId": "",
    })
    if claim == "pull":
        return {"ok": False, "error": "拉取进行中，请稍后再同步", "job": sync_job_snapshot()}
    if claim:
        return {"ok": False, "error": "已有同步任务进行中", "job": sync_job_snapshot()}

    thread = threading.Thread(
        target=_run_sync_job,
        args=(cid, message, only, full),
        name="dzmm-sync",
        daemon=True,
    )
    thread.start()
    return {"ok": True, "message": "同步已开始", "job": sync_job_snapshot()}


def do_card_list() -> dict:
    try:
        return {
            "ok": True,
            "cardsDir": str(character.cards_root()),
            "items": character.list_local_cards(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}


def do_card_chat_prompt(name: str = "", brief: str = "") -> dict:
    try:
        data = character.build_chat_prompt(name=name, brief=brief)
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_play_meta(card_id: int) -> dict:
    try:
        data = character.play_meta(int(card_id))
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_play_delete(body: dict) -> dict:
    """删除试玩会话：tRPC chat.deleteChat。"""
    try:
        chat_id = str((body or {}).get("chatId") or "").strip()
        if not chat_id:
            return {"ok": False, "error": "缺少 chatId"}
        data = character.play_delete_chat(chat_id)
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_play_start(body: dict) -> dict:
    try:
        card_id = int(body.get("cardId") or 0)
        idx = body.get("chatHistoryIndex")
        if idx is not None and idx != "":
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                idx = None
        else:
            idx = None
        data = character.play_start(card_id, chat_history_index=idx)
        # 拉首轮消息（开场）+ 会话设置
        msgs = {}
        try:
            msgs = character.play_messages(data["chatId"])
        except Exception as e:
            msgs = {"error": str(e)}
        settings = {}
        try:
            settings = character.play_get_settings(data["chatId"])
        except Exception as e:
            settings = {"error": str(e)}
        flat = character._flatten_play_messages(msgs if isinstance(msgs, dict) else {})
        return {
            "ok": True,
            **data,
            "messages": flat,
            "rawMessages": msgs,
            "settings": settings if isinstance(settings, dict) else {},
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_play_models() -> dict:
    try:
        data = character.play_models()
        return {"ok": True, "models": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_play_presets() -> dict:
    try:
        data = character.play_presets()
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_play_messages(chat_id: str) -> dict:
    try:
        data = character.play_messages(chat_id)
        flat = character._flatten_play_messages(data if isinstance(data, dict) else {})
        return {"ok": True, "messages": flat, "raw": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_play_settings_get(chat_id: str) -> dict:
    try:
        data = character.play_get_settings(chat_id)
        return {"ok": True, "settings": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_play_settings_update(body: dict) -> dict:
    try:
        chat_id = str(body.get("chatId") or "").strip()
        settings = body.get("settings")
        if not isinstance(settings, dict):
            settings = {k: v for k, v in body.items() if k != "chatId"}
        data = character.play_update_settings(chat_id, settings)
        # 再拉一遍最新设置
        fresh = {}
        try:
            fresh = character.play_get_settings(chat_id)
        except Exception:
            fresh = data if isinstance(data, dict) else {}
        return {"ok": True, "settings": fresh, "raw": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _proxy_card_play_generate(handler, body: dict) -> None:
    """把 POST /api/chat 的 SSE 流式结果转给前端。"""
    try:
        chat_id = str(body.get("chatId") or "").strip()
        card_id = body.get("cardId")
        content = str(body.get("content") or "")
        if not chat_id:
            _json(handler, 400, {"ok": False, "error": "缺少 chatId"})
            return
        if card_id is None or card_id == "":
            _json(handler, 400, {"ok": False, "error": "缺少 cardId"})
            return
        if not content.strip():
            _json(handler, 400, {"ok": False, "error": "消息不能为空"})
            return
        preset_ids = body.get("presetIds")
        if not isinstance(preset_ids, list):
            preset_ids = []
        prompts = body.get("prompts")
        if not isinstance(prompts, list):
            prompts = []
        gen_body = character.build_generate_body(
            chat_id=chat_id,
            card_id=card_id,
            content=content,
            model=body.get("model") or None,
            max_tokens=body.get("maxTokens"),
            deep_thinking=bool(body.get("deepThinking")),
            enable_memory_enhance=bool(body.get("enableMemoryEnhance")),
            style=body.get("style") or None,
            image_generation_model=body.get("imageGenerationModel") or None,
            preset_ids=[str(x) for x in preset_ids],
            player_info=body.get("playerInfo") or None,
            prompts=prompts,
        )
        st, resp, hdr = character.play_generate_request(gen_body)
        if st != 200:
            err_raw = b""
            try:
                err_raw = resp.read() if hasattr(resp, "read") else b""
            except Exception:
                pass
            try:
                err_obj = json.loads(err_raw.decode("utf-8", "replace"))
            except Exception:
                err_obj = {"error": err_raw.decode("utf-8", "replace")[:800]}
            _json(handler, st if st >= 400 else 400, {"ok": False, "status": st, **(err_obj if isinstance(err_obj, dict) else {"error": err_obj})})
            return

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache, no-transform")
        handler.send_header("Connection", "close")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()

        # 透传上游 SSE；同时规范化方便前端
        try:
            if hasattr(resp, "fp") and hasattr(resp.fp, "read"):
                # HTTPResponse: iter lines
                buf = b""
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        handler.wfile.write(line + b"\n")
                        handler.wfile.flush()
                if buf:
                    handler.wfile.write(buf)
                    handler.wfile.flush()
            else:
                data = resp.read() if hasattr(resp, "read") else b""
                handler.wfile.write(data)
                handler.wfile.flush()
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except Exception as e:
        if not handler.wfile.closed:
            try:
                _json(handler, 500, {"ok": False, "error": str(e)})
            except Exception:
                pass


def do_card_get(local_id: str) -> dict:
    try:
        card = character.load_local(local_id)
        lid = (card.get("_meta") or {}).get("localId") or local_id
        return {
            "ok": True,
            "localId": lid,
            "folder": (card.get("_meta") or {}).get("folder") or "",
            "mtime": float((card.get("_meta") or {}).get("mtime") or 0),
            "sig": character.folder_fingerprint(str(lid)),
            "card": card,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_new(body: dict) -> dict:
    try:
        # 空白新建：必须带卡名；不沿用上一张卡的简述
        blank = body.get("blank")
        if blank is None:
            blank = True
        blank = bool(blank)
        name = str(body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "必须填写卡名才能新建"}
        brief = "" if blank else str(body.get("brief") or "").strip()
        saved = character.create_local(name, brief=brief, blank=blank)
        return {
            "ok": True,
            "localId": saved["localId"],
            "path": saved["path"],
            "folder": saved["path"],
            "mtime": saved.get("mtime") or 0,
            "card": saved["card"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_save(body: dict) -> dict:
    try:
        card = body.get("card")
        local_id = body.get("localId")
        if not isinstance(card, dict):
            return {"ok": False, "error": "缺少 card 对象"}
        # 允许前端只提交 data 字段
        if "data" not in card and any(k in card for k in ("name", "description", "personality")):
            base = character.empty_card(str(card.get("name") or "未命名角色"))
            for k in (
                "name",
                "description",
                "personality",
                "scenario",
                "first_mes",
                "tags",
                "creator_notes",
            ):
                if k in card:
                    base["data"][k] = card[k]
            if isinstance(body.get("meta"), dict):
                base["_meta"].update(body["meta"])
            card = base
        brief = str(body.get("brief") or (card.get("_meta") or {}).get("brief") or "")
        prev = str(body.get("previousLocalId") or body.get("fromLocalId") or "").strip() or None
        force = bool(body.get("force") or body.get("forceOverwrite"))
        saved = character.save_local(
            card,
            local_id=str(local_id or "").strip() or None,
            previous_local_id=prev,
            force=force,
        )
        # 再写一遍 brief（save_local 已带 meta.brief；这里保证 body.brief 优先生效）
        if brief:
            saved = character.write_folder(saved["card"], local_id=saved["localId"], brief=brief)
        return {
            "ok": True,
            "localId": saved["localId"],
            "path": saved["path"],
            "folder": saved["path"],
            "mtime": saved.get("mtime") or 0,
            "card": saved["card"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_ai(body: dict) -> dict:
    """不再调平台 AI：按卡名创建/打开「卡/<名>/」，供本地编辑 txt。"""
    try:
        brief = str(body.get("brief") or "").strip()
        name = str(body.get("name") or "").strip()
        if not name:
            # 从简述第一行猜卡名
            first = (brief.splitlines()[0] if brief else "").strip()
            name = first[:40] if first else "未命名角色"
        saved = character.prepare_workspace(name, brief=brief)
        return {
            "ok": True,
            "localId": saved["localId"],
            "path": saved["path"],
            "folder": saved["path"],
            "mtime": saved.get("mtime") or 0,
            "card": saved["card"],
            "created": bool(saved.get("created")),
            "hint": f"请编辑本地文件：{saved['path']}\\*.txt（网页会实时同步）",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_poll(local_id: str, since_mtime: float = 0.0, since_sig: str = "") -> dict:
    try:
        data = character.poll_local(local_id, since_mtime=since_mtime, since_sig=since_sig)
        data["ok"] = True
        return data
    except Exception as e:
        return {"ok": False, "error": str(e), "changed": False}


def do_card_avatar(body: dict) -> dict:
    try:
        local_id = str(body.get("localId") or body.get("name") or "").strip()
        if not local_id:
            return {"ok": False, "error": "需要 localId（卡名）"}
        data_b64 = str(body.get("dataBase64") or body.get("data") or "")
        if not data_b64:
            return {"ok": False, "error": "缺少图片 dataBase64"}
        saved = character.save_local_avatar_b64(
            local_id,
            data_b64,
            filename=str(body.get("filename") or ""),
            mime=str(body.get("mime") or ""),
        )
        return {
            "ok": True,
            "localId": saved["localId"],
            "path": saved["path"],
            "folder": saved.get("folder") or saved["path"],
            "mtime": saved.get("mtime") or 0,
            "rel": saved.get("rel"),
            "avatarUrl": saved.get("avatarUrl"),
            "serveUrl": saved.get("serveUrl"),
            "card": saved["card"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_image(body: dict) -> dict:
    try:
        local_id = str(body.get("localId") or body.get("name") or "").strip()
        if not local_id:
            return {"ok": False, "error": "需要 localId（卡名）"}
        action = str(body.get("action") or "add").strip().lower()
        if action == "remove":
            saved = character.remove_local_image(local_id, int(body.get("index")))
            return {
                "ok": True,
                "localId": saved["localId"],
                "path": saved["path"],
                "folder": saved.get("folder") or saved["path"],
                "mtime": saved.get("mtime") or 0,
                "card": saved["card"],
            }
        data_b64 = str(body.get("dataBase64") or body.get("data") or "")
        if not data_b64:
            return {"ok": False, "error": "缺少图片 dataBase64"}
        saved = character.save_local_image(
            local_id,
            data_b64=data_b64,
            filename=str(body.get("filename") or ""),
            mime=str(body.get("mime") or ""),
            name=str(body.get("name") or ""),
            set_avatar=bool(body.get("setAvatar")),
        )
        return {
            "ok": True,
            "localId": saved["localId"],
            "path": saved["path"],
            "folder": saved.get("folder") or saved["path"],
            "mtime": saved.get("mtime") or 0,
            "rel": saved.get("rel"),
            "serveUrl": saved.get("serveUrl"),
            "index": saved.get("index"),
            "card": saved["card"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_voices() -> dict:
    try:
        data = character.list_platform_voices()
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "error": str(e), "public": [], "mine": []}


def do_card_voice(body: dict) -> dict:
    try:
        local_id = str(body.get("localId") or "").strip()
        if not local_id:
            return {"ok": False, "error": "需要 localId"}
        if body.get("clear"):
            saved = character.set_voice_settings(local_id, None)
        else:
            voice = body.get("voice")
            if not isinstance(voice, dict) or not voice.get("id"):
                return {"ok": False, "error": "需要 voice 对象（含 id/name）"}
            saved = character.set_voice_settings(
                local_id,
                voice,
                settings=body.get("settings") if isinstance(body.get("settings"), dict) else None,
            )
        return {
            "ok": True,
            "localId": saved["localId"],
            "path": saved["path"],
            "folder": saved.get("folder") or saved["path"],
            "mtime": saved.get("mtime") or 0,
            "card": saved["card"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_export_png(local_id: str) -> dict:
    try:
        local_id = str(local_id or "").strip()
        if not local_id:
            return {"ok": False, "error": "需要 localId"}
        result = character.export_card_png(local_id, save_copy=True)
        return {"ok": True, **{k: v for k, v in result.items() if k != "bytes"}, "bytes": result["bytes"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_delete_local(body: dict) -> dict:
    try:
        local_id = str(body.get("localId") or "").strip()
        if not local_id:
            return {"ok": False, "error": "需要 localId"}
        result = character.delete_local_card(local_id)
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_delete_cloud(body: dict) -> dict:
    """删云端草稿 / 下线已发布卡（可级联清同卡草稿）。"""
    try:
        cloud_id = int(body.get("cloudId") or body.get("id") or 0)
        if cloud_id <= 0:
            return {"ok": False, "error": "需要有效 cloudId"}
        is_draft = body.get("isDraft")
        if is_draft is not None:
            is_draft = bool(is_draft)
        character_id = body.get("characterId") or body.get("dbId")
        if character_id is not None:
            try:
                character_id = int(character_id)
            except (TypeError, ValueError):
                character_id = None
        also_hide = body.get("alsoHidePublished")
        if also_hide is None:
            also_hide = False
        cascade = body.get("cascadeDrafts")
        if cascade is None:
            cascade = True
        result = character.remove_cloud_card(
            cloud_id=cloud_id,
            is_draft=is_draft,
            also_hide_published=bool(also_hide),
            cascade_drafts=bool(cascade),
            character_id=character_id,
        )
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_publish(body: dict) -> dict:
    try:
        local_id = str(body.get("localId") or "").strip()
        if not local_id:
            return {"ok": False, "error": "需要 localId"}
        # 若前端带了最新表单，先落盘再发
        card = body.get("card")
        if isinstance(card, dict):
            brief = str(body.get("brief") or (card.get("_meta") or {}).get("brief") or "")
            character.save_local(card, local_id=local_id)
            if brief:
                character.write_folder(
                    character.load_local(local_id),
                    local_id=local_id,
                    brief=brief,
                )
        as_draft = bool(body.get("draft") or body.get("asDraft"))
        saved = character.publish_to_cloud(local_id, as_draft=as_draft)
        return {
            "ok": True,
            "localId": saved["localId"],
            "path": saved["path"],
            "folder": saved.get("folder") or saved["path"],
            "mtime": saved.get("mtime") or 0,
            "card": saved["card"],
            "mode": saved.get("mode"),
            "cloudId": saved.get("cloudId"),
            "characterUrl": saved.get("characterUrl") or "",
            "result": saved.get("result"),
            "clearedDrafts": saved.get("clearedDrafts") or [],
            "clearedDraftCount": int(saved.get("clearedDraftCount") or 0),
            "draftSync": saved.get("draftSync") or {},
            "syncedDraftId": saved.get("syncedDraftId"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_card_shelf(body: dict) -> dict:
    """广场上架：card.publish。下架请到官网。"""
    try:
        card_id = int(
            body.get("cardId")
            or body.get("cloudId")
            or body.get("id")
            or 0
        )
        if card_id <= 0:
            return {"ok": False, "error": "需要有效 cardId（先「保存到云端」拿到正式卡）"}
        listed = body.get("listed")
        if listed is None:
            action = str(body.get("action") or "publish").strip().lower()
            listed = action in ("publish", "shelf", "list", "上架")
        if listed is False or (isinstance(listed, str) and listed.lower() in ("false", "0", "unpublish", "unshelf")):
            return {"ok": False, "error": "控制台不支持下架，请到官网自行操作"}
        local_id = str(body.get("localId") or "").strip() or None
        result = character.shelf_cloud_card(
            card_id,
            listed=True,
            local_id=local_id,
        )
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _serve_card_asset(handler: BaseHTTPRequestHandler, local_id: str, rel: str) -> None:
    try:
        file_path = character.resolve_card_asset(local_id, rel)
    except Exception as e:
        _json(handler, 404, {"ok": False, "error": str(e)})
        return
    ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    raw = file_path.read_bytes()
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(raw)
    except _CLIENT_DISCONNECT:
        pass


def do_card_cloud_list(quick: bool = False) -> dict:
    try:
        items = character.list_cloud_cards(enrich_listing=not quick)
        return {"ok": True, "items": items, "quick": bool(quick)}
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}


def do_card_pull_cloud(body: dict) -> dict:
    try:
        cloud_id = int(body.get("cloudId") or 0)
        if cloud_id <= 0:
            return {"ok": False, "error": "需要有效 cloudId"}
        folder_name = str(body.get("name") or body.get("folderName") or "").strip() or None
        is_draft = body.get("isDraft")
        if is_draft is not None:
            is_draft = bool(is_draft)
        saved = character.pull_cloud_card(cloud_id, folder_name=folder_name, is_draft=is_draft)
        return {
            "ok": True,
            "localId": saved["localId"],
            "path": saved["path"],
            "folder": saved.get("folder") or saved["path"],
            "mtime": saved.get("mtime") or 0,
            "card": saved["card"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_start_preview(body: dict) -> dict:
    global _PREVIEW_PROC
    status = build_status()
    if not status["loggedIn"]:
        return {"ok": False, "error": "尚未登录", "status": status}

    cid = body.get("characterId")
    if cid is None or str(cid).strip() == "":
        cid = status["characterId"]
    try:
        cid = int(cid or 0)
    except Exception:
        cid = 0
    if not cid:
        return {"ok": False, "error": "请先填写 character_id", "status": status}

    project = str(body.get("projectPath") or status.get("projectPath") or "").strip()
    port = body.get("previewPort")
    if port is None or str(port).strip() == "":
        port = status.get("previewPort") or 8791
    try:
        port = int(port)
    except Exception:
        port = 8791

    if not project:
        project = str(puller.default_out_dir(cid))
    global _PREVIEW_SOURCE
    source = str(body.get("source") or preview_source_get() or "local").strip().lower()
    if source not in ("local", "cloud"):
        source = "local"

    resolved = studio.resolve_game_project(project)
    if source == "local" and not resolved.get("ok"):
        err = resolved.get("error") or "本地项目无法预览"
        failed_n = len(resolved.get("failedPaths") or [])
        return {
            "ok": False,
            "error": err,
            "canRetryFailed": failed_n > 0,
            "failedCount": failed_n,
            "status": build_status(),
        }

    project_path = Path(resolved["root"]) if resolved.get("ok") else Path(project).expanduser()
    publish_dir = Path(resolved["publish_dir"]) if resolved.get("ok") else (project_path / "publish")
    hint = str(resolved.get("hint") or "") if resolved.get("ok") else ""
    _PREVIEW_SOURCE = source
    if source == "cloud":
        mirror_root = cloud_mirror_root(cid)
        cloud_pub = mirror_root / "publish"
        if not (cloud_pub / "index.html").is_file():
            return {
                "ok": False,
                "pending": True,
                "error": "云端镜像尚未就绪（缺少 publish/index.html），请稍候或先点「云端」预览",
                "status": build_status(),
            }
        # 预览进程指向旁路目录，避免覆盖本地工程
        project_path = mirror_root
        publish_dir = cloud_pub
        hint = (("云端镜像 · " + hint) if hint else "云端镜像预览").strip(" ·")

    cfg_root = str(resolved["root"]) if resolved.get("ok") else str(Path(project).expanduser())
    studio.save_config({
        "character_id": cid,
        "project_path": cfg_root,  # 配置仍记本地工程根
        "preview_port": port,
    })

    script = KIT / "lib" / "dzmm_preview_server.py"
    cmd = [
        sys.executable,
        str(script),
        "--character-id",
        str(cid),
        "--port",
        str(port),
        "--project-root",
        str(project_path),
        "--publish-dir",
        str(publish_dir),
        "--no-open",
    ]

    log_path = KIT / ".preview-server.log"
    try:
        log_fp = open(log_path, "w", encoding="utf-8", errors="replace")
    except OSError:
        log_fp = subprocess.DEVNULL

    with _PREVIEW_LOCK:
        _stop_preview_locked()
        try:
            # 日志写文件，避免 PIPE 塞满导致预览线程卡死 → 浏览器 ERR_EMPTY_RESPONSE
            proc = subprocess.Popen(
                cmd,
                cwd=str(KIT),
                stdout=log_fp,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            if log_fp is not subprocess.DEVNULL:
                try:
                    log_fp.close()
                except Exception:
                    pass
            return {"ok": False, "error": f"无法启动预览：{e}", "status": build_status()}

        _PREVIEW_PROC = proc
        url = f"http://127.0.0.1:{port}/"
        boot_msg = "正在启动预览…"
        if hint:
            boot_msg = f"{hint} · {boot_msg}"
        _PREVIEW_META.update({
            "running": True,
            "port": port,
            "url": url,
            "characterId": cid,
            "projectPath": str(project_path),
            "publishDir": str(publish_dir),
            "pid": proc.pid,
            "error": "",
            "message": boot_msg,
            "logPath": str(log_path),
            "source": source,
        })

    # 用 /health 探测就绪，不再依赖 stdout 管道
    ready = False
    deadline = time.time() + 20
    last_err = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            err = f"预览启动失败 exit={proc.returncode}"
            try:
                if log_path.is_file():
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:].strip()
                    if tail:
                        err = tail
            except Exception:
                pass
            with _PREVIEW_LOCK:
                _PREVIEW_PROC = None
                _PREVIEW_META.update({"running": False, "pid": 0, "error": err, "message": err})
            return {"ok": False, "error": err, "preview": preview_snapshot(), "status": build_status()}
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception as e:
            last_err = str(e)
        time.sleep(0.15)

    with _PREVIEW_LOCK:
        if ready:
            _PREVIEW_META["message"] = f"预览已就绪 {url}"
        else:
            _PREVIEW_META["message"] = f"预览进程已启动（等待端口就绪）{(' · ' + last_err) if last_err else ''}"

    return {
        "ok": True,
        "url": url,
        "preview": preview_snapshot(),
        "status": build_status(),
    }


# 高频成功请求不刷终端；非 2xx 仍打印
_QUIET_OK_PATH_PREFIXES = (
    "/api/card/poll",
    "/api/card/list",
    "/api/card/cloud",
    "/api/status",
    "/api/origin",
    "/api/preview",
    "/api/bridge",
    "/api/ping",
    "/api/pull",
    "/api/sync",
    "/api/console-mode",
)


class Handler(BaseHTTPRequestHandler):
    server_version = "DZMM-LocalDev/1.0"

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:
            msg = str(fmt)
        path = (getattr(self, "path", None) or "").split("?", 1)[0]
        # BaseHTTPRequestHandler 典型格式: "GET /path HTTP/1.1" 200 -
        ok = (" 200 " in f" {msg} ") or msg.rstrip().endswith(" 200 -")
        if ok and any(path == p or path.startswith(p + "/") for p in _QUIET_OK_PATH_PREFIXES):
            return
        if ok and (path.startswith("/assets/") or path.endswith((".js", ".css", ".ico", ".png", ".svg", ".woff2"))):
            return
        sys.stderr.write("[console] " + msg + "\n")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        if path == "/api/status":
            payload = build_status()
            payload["pull"] = pull_job_snapshot()
            payload["sync"] = sync_job_snapshot()
            payload["preview"] = preview_snapshot()
            _json(self, 200, payload)
            return
        if path == "/api/origin":
            _json(self, 200, do_origin_get())
            return
        if path == "/api/console-mode":
            _json(self, 200, do_console_mode_get())
            return
        if path == "/api/ping":
            _json(self, 200, do_ping_editor())
            return
        if path == "/api/pull":
            _json(self, 200, {"ok": True, "job": pull_job_snapshot()})
            return
        if path == "/api/sync":
            _json(self, 200, {"ok": True, "job": sync_job_snapshot()})
            return
        if path == "/api/preview":
            _json(self, 200, {"ok": True, "preview": preview_snapshot(), "source": preview_source_get(), "cloud": cloud_mirror_snapshot()})
            return
        if path == "/api/preview/source":
            _json(self, 200, do_preview_source_get())
            return
        if path == "/api/bridge":
            qs = urllib.parse.parse_qs(parsed.query or "")
            force = str((qs.get("force") or ["0"])[0]).lower() in ("1", "true", "yes")
            _json(self, 200, do_bridge_status(force=force))
            return
        if path == "/api/bridge/publish":
            _json(self, 200, do_bridge_publish_status())
            return
        if path == "/api/card/list":
            _json(self, 200, do_card_list())
            return
        if path == "/api/card/chat-prompt":
            qs = urllib.parse.parse_qs(parsed.query or "")
            name = (qs.get("name") or [""])[0]
            brief = (qs.get("brief") or [""])[0]
            result = do_card_chat_prompt(name=name, brief=brief)
            _json(self, 200 if result.get("ok") else 404, result)
            return
        if path == "/api/card/get":
            qs = urllib.parse.parse_qs(parsed.query or "")
            local_id = (qs.get("id") or [""])[0]
            result = do_card_get(local_id)
            _json(self, 200 if result.get("ok") else 404, result)
            return
        if path == "/api/card/cloud":
            qs = urllib.parse.parse_qs(parsed.query or "")
            quick_raw = (qs.get("quick") or ["0"])[0]
            quick = str(quick_raw).lower() in ("1", "true", "yes")
            result = do_card_cloud_list(quick=quick)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/poll":
            qs = urllib.parse.parse_qs(parsed.query or "")
            local_id = (qs.get("id") or [""])[0]
            try:
                since = float((qs.get("since") or ["0"])[0] or 0)
            except Exception:
                since = 0.0
            since_sig = (qs.get("sig") or [""])[0] or ""
            result = do_card_poll(local_id, since_mtime=since, since_sig=since_sig)
            _json(self, 200 if result.get("ok") else 404, result)
            return
        if path == "/api/card/asset":
            qs = urllib.parse.parse_qs(parsed.query or "")
            local_id = (qs.get("id") or [""])[0]
            rel = (qs.get("path") or [""])[0]
            _serve_card_asset(self, local_id, rel)
            return
        if path == "/api/card/export-png":
            qs = urllib.parse.parse_qs(parsed.query or "")
            local_id = (qs.get("id") or [""])[0]
            result = do_card_export_png(local_id)
            if not result.get("ok"):
                _json(self, 400, {k: v for k, v in result.items() if k != "bytes"})
                return
            raw = result["bytes"]
            filename = str(result.get("filename") or "card.png")
            # RFC 5987 文件名
            star = urllib.parse.quote(filename)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"card.png\"; filename*=UTF-8''{star}",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/card/voices":
            result = do_card_voices()
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/meta":
            qs = urllib.parse.parse_qs(parsed.query or "")
            try:
                card_id = int((qs.get("cardId") or ["0"])[0] or 0)
            except Exception:
                card_id = 0
            result = do_card_play_meta(card_id)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/models":
            result = do_card_play_models()
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/presets":
            result = do_card_play_presets()
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/messages":
            qs = urllib.parse.parse_qs(parsed.query or "")
            chat_id = (qs.get("chatId") or [""])[0]
            result = do_card_play_messages(chat_id)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/settings":
            qs = urllib.parse.parse_qs(parsed.query or "")
            chat_id = (qs.get("chatId") or [""])[0]
            result = do_card_play_settings_get(chat_id)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/agent/ready":
            _json(self, 200, do_agent_ready({}))
            return
        if path == "/api/agent/sessions":
            qs = urllib.parse.parse_qs(parsed.query or "")
            backend = (qs.get("backend") or ["claude"])[0]
            result = do_agent_sessions(backend)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/agent/messages":
            qs = urllib.parse.parse_qs(parsed.query or "")
            session_id = (qs.get("sessionId") or [""])[0]
            backend = (qs.get("backend") or ["claude"])[0]
            result = do_agent_messages(session_id, backend)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        self._serve_static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        body = _read_body(self)
        if path == "/api/login":
            result = do_login(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/login/cookie":
            result = do_login_cookie(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/login/code":
            result = do_login_code(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/login/telegram/start":
            result = do_login_telegram_start(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/login/telegram/poll":
            result = do_login_telegram_poll(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/origin":
            result = do_origin_set(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/console-mode":
            result = do_console_mode_set(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/config":
            result = do_save_config(body)
            _json(self, 200, result)
            return
        if path == "/api/config/history/remove":
            result = do_remove_project_history(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/logout":
            _json(self, 200, do_logout())
            return
        if path == "/api/ping":
            result = do_ping_editor()
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/pull":
            result = do_start_pull(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/pull/retry":
            result = do_retry_failed_pull(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/preview/start":
            result = do_start_preview(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/preview/stop":
            _stop_cloud_mirror()
            global _PREVIEW_SOURCE
            _PREVIEW_SOURCE = "local"
            _json(self, 200, do_stop_preview())
            return
        if path == "/api/preview/source":
            result = do_preview_source_set(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/bridge/publish":
            result = do_bridge_publish(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/bridge/reload":
            result = do_bridge_auth_reload()
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/sync":
            result = do_start_sync(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/new":
            result = do_card_new(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/save":
            result = do_card_save(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/ai":
            result = do_card_ai(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/pull":
            result = do_card_pull_cloud(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/avatar":
            result = do_card_avatar(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/image":
            result = do_card_image(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/voice":
            result = do_card_voice(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/publish":
            result = do_card_publish(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/shelf":
            result = do_card_shelf(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/delete":
            result = do_card_delete_local(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/cloud/delete":
            result = do_card_delete_cloud(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/start":
            result = do_card_play_start(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/delete":
            result = do_card_play_delete(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/settings":
            result = do_card_play_settings_update(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/card/play/send":
            _proxy_card_play_generate(self, body)
            return
        if path == "/api/agent/ready":
            result = do_agent_ready(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/agent/send":
            result = do_agent_send(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/agent/poll":
            result = do_agent_poll(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/agent/cancel":
            result = do_agent_cancel(body)
            _json(self, 200 if result.get("ok") else 400, result)
            return
        _json(self, 404, {"ok": False, "error": "not found"})

    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/").replace("\\", "/")
        if ".." in rel.split("/"):
            self.send_error(400)
            return
        file_path = (WEB_DIR / rel).resolve()
        if not str(file_path).startswith(str(WEB_DIR.resolve())) or not file_path.is_file():
            self.send_error(404)
            return
        data = file_path.read_bytes()
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except _CLIENT_DISCONNECT:
            pass


def _try_auto_preview() -> None:
    """登录且本地 publish 就绪时自动拉起预览（供 start.py --auto-preview）。"""
    time.sleep(0.35)
    try:
        if _console_mode_from_cfg() == "card":
            print("[console] 自动预览跳过：当前偏好角色卡模式（只需登录态写卡）")
            return
        status = build_status()
        if not status.get("loggedIn"):
            print("[console] 自动预览跳过：尚未登录（网页登录后可手动启动预览）")
            return
        if not status.get("publishIndexExists"):
            detail = status.get("projectResolveError") or "本地尚无可预览的 index.html"
            print(f"[console] 自动预览跳过：{detail.splitlines()[0]}")
            return
        print("[console] 自动启动游戏预览…")
        result = do_start_preview({})
        if result.get("ok"):
            snap = result.get("preview") or {}
            print(f"[console] 预览已就绪 {snap.get('url') or ''}".rstrip())
        else:
            print(f"[console] 自动预览失败：{result.get('error') or 'unknown'}")
    except Exception as e:
        print(f"[console] 自动预览异常：{e}")


def main():
    ap = argparse.ArgumentParser(description="DZMM local dev web console")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument(
        "--auto-preview",
        action="store_true",
        help="启动后若已登录且项目就绪则自动打开本地预览",
    )
    args = ap.parse_args()

    studio.refresh_root()
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"[console] DZMM 本地开发控制台: {url}")
    print("[console] 在网页填写邮箱/密码/character_id 后登录即可")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if args.auto_preview:
        threading.Thread(target=_try_auto_preview, name="auto-preview", daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] stopped")
    finally:
        try:
            do_stop_preview()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
