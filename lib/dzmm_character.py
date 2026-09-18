#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色卡模块：本地「卡/<卡名>/」按平台分类落盘，并实时读盘同步。

分类对齐 studio/edit：基础 / 世界书 / 对话 / 图片音色。
不用平台 AI。与 Game Studio 游戏容器分离。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import struct
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import dzmm_studio as studio

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

_FLIGHT_BOOL = {"!0": True, "!1": False, "true": True, "false": False}
_PENDING_STATUSES = frozenset({"pending", "pending_notified"})
_LISTED_STATUSES = frozenset({"approved"})

_AVATAR_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

KIT_ROOT = Path(__file__).resolve().parents[1]
CARDS_DIR = KIT_ROOT / "卡"
TEMPLATE_DIR = KIT_ROOT / "_模板"
CHAT_PROMPT_PATH = TEMPLATE_DIR / "开聊提示词.txt"

# 官网「互动模板」extensions.status_template（studio/edit 专业模式）
STATUS_TEMPLATE_IDS = ("status_bar", "relationship", "inventory", "options")
STATUS_TEMPLATE_FIELD_HOSTS = ("status_bar", "relationship", "inventory")
STATUS_TEMPLATE_FIELD_TYPES = ("text", "number", "meter", "list")
STATUS_TEMPLATE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
STATUS_TEMPLATE_DEFAULTS: dict[str, list[dict]] = {
    "status_bar": [
        {"key": "time", "template": "status_bar", "label": "时间", "type": "text", "initial": "未知"},
        {"key": "location", "template": "status_bar", "label": "地点", "type": "text", "initial": "未知"},
        {"key": "mood", "template": "status_bar", "label": "心情", "type": "text", "initial": "平静"},
    ],
    "relationship": [
        {
            "key": "favor",
            "template": "relationship",
            "label": "好感度",
            "type": "meter",
            "min": 0,
            "max": 100,
            "initial": 20,
            "hint": "按本轮剧情在当前值基础上合理增减，单轮变化不超过 10",
        },
        {
            "key": "stage",
            "template": "relationship",
            "label": "关系阶段",
            "type": "text",
            "initial": "陌生",
            "hint": "按好感度自动：低于 30 陌生，30~59 熟人，60~84 暧昧，85 以上恋人",
        },
    ],
    "inventory": [
        {"key": "inventory", "template": "inventory", "label": "背包", "type": "list", "initial": []},
    ],
}


def _require_auth(min_remain: int = 30):
    """load_auth 失败会 SystemExit；统一转成 RuntimeError，供卡接口 Exception 捕获。"""
    try:
        return studio.load_auth(min_remain=min_remain)
    except SystemExit as e:
        raise RuntimeError(str(e) or "未登录或鉴权失败") from e


def _int_field(value, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_chat_prompt(name: str = "", brief: str = "") -> dict:
    """读取根目录 `_模板/开聊提示词.txt`，用当前卡名/简述填好占位，便于一键复制。"""
    if not CHAT_PROMPT_PATH.is_file():
        raise FileNotFoundError(f"找不到开聊提示词：{CHAT_PROMPT_PATH}")
    text = CHAT_PROMPT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    name = (name or "").strip()
    brief = (brief or "").strip()
    if name:
        # 路径占位优先，再替卡名文案
        text = text.replace("卡/<卡名>/", f"卡/{name}/")
        text = re.sub(r"卡/<[^>\n]+>/", f"卡/{name}/", text)
        text = text.replace("这里写卡名", name)
        text = text.replace("<卡名>", name)
        text = text.replace("「卡名」", f"「{name}」")
        # 旧模板没有【任务】句时补一句，避免 AI 不知道写哪张
        if f"现在开始创作角色卡「{name}」" not in text:
            text = (
                f"【任务】现在开始创作角色卡「{name}」。\n"
                f"目标目录：卡/{name}/\n\n"
                + text
            )
    if brief:
        # 覆盖「（这里写创意简述…）」整段括号，或单独占位句
        text = re.sub(
            r"（这里写创意简述[^）]*）",
            brief,
            text,
            count=1,
        )
        if "这里写创意简述" in text:
            text = text.replace("这里写创意简述：定位、时代/场景、关系与冲突、语气与尺度", brief)
            text = text.replace("这里写创意简述", brief)
    return {
        "text": text.strip() + "\n",
        "path": str(CHAT_PROMPT_PATH),
        "name": name,
        "brief": brief,
        "filledName": bool(name),
        "filledBrief": bool(brief),
    }

# 基础文本字段 → 文件名
BASIC_TXT = (
    "name",
    "description",
    "personality",
    "scenario",
    "system_prompt",
    "creator_notes",
    "tags",
    "creator",
    "character_version",
    "first_mes",
    "brief",
    "avatar_url",
)


def _is_user_card_dir(path: Path) -> bool:
    """是否为用户角色卡夹（排除隐藏目录与 _模板 等下划线前缀目录）。"""
    if not path.is_dir():
        return False
    name = path.name
    if not name or name.startswith(".") or name.startswith("_"):
        return False
    return True


def cards_root() -> Path:
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    return CARDS_DIR


def empty_card(name: str = "未命名角色") -> dict:
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": name,
            "description": "",
            "personality": "",
            "scenario": "",
            "first_mes": "",
            "system_prompt": "",
            "creator_notes": "",
            "tags": [],
            "creator": "",
            "character_version": "",
            "alternate_greetings": [],
            "suggested_replies": [],
            "avatar_url": "",
            "image_info": [],
            "chat_history": [],
            "character_book": {"name": "世界设定", "entries": [], "extensions": {}},
            "voice_settings": None,
            "extensions": {},
            "db_id": -1,
        },
        "_meta": {
            "localId": "",
            "folder": "",
            "updatedAt": "",
            "source": "local-folder",
            "cloudId": None,
            "brief": "",
        },
    }


def _safe_folder_name(name: str) -> str:
    raw = (name or "").strip()
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", raw)
    raw = raw.strip(" .")
    return (raw or "未命名角色")[:80]


def _status_template_defaults_for(template_id: str) -> list[dict]:
    raw = STATUS_TEMPLATE_DEFAULTS.get(template_id) or []
    return [json.loads(json.dumps(x)) for x in raw]


def normalize_status_template(raw) -> dict | None:
    """规范化官网 extensions.status_template；无效/空则返回 None。"""
    if not isinstance(raw, dict):
        return None
    templates_in = raw.get("templates") if isinstance(raw.get("templates"), list) else []
    templates: list[str] = []
    seen_t: set[str] = set()
    for t in templates_in:
        tid = str(t or "").strip()
        if tid in STATUS_TEMPLATE_IDS and tid not in seen_t:
            templates.append(tid)
            seen_t.add(tid)

    panels_in = raw.get("panels") if isinstance(raw.get("panels"), list) else []
    panels: list[dict] = []
    panel_ids: set[str] = set()
    reserved = set(STATUS_TEMPLATE_IDS)
    for p in panels_in[:6]:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        title = str(p.get("title") or "").strip()[:12]
        if not STATUS_TEMPLATE_KEY_RE.match(pid) or pid in reserved or pid in panel_ids:
            continue
        panels.append({"id": pid, "title": title})
        panel_ids.add(pid)

    allowed_hosts = {t for t in templates if t in STATUS_TEMPLATE_FIELD_HOSTS} | panel_ids
    fields_in = raw.get("fields") if isinstance(raw.get("fields"), list) else []
    fields: list[dict] = []
    seen_keys: set[str] = set()
    for f in fields_in[:32]:
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "").strip()
        host = str(f.get("template") or "").strip()
        label = str(f.get("label") or "").strip()[:20]
        ftype = str(f.get("type") or "text").strip()
        if not STATUS_TEMPLATE_KEY_RE.match(key) or key in seen_keys:
            continue
        if host not in allowed_hosts or ftype not in STATUS_TEMPLATE_FIELD_TYPES:
            continue
        # 字段名可暂时为空（对齐官网「请填写字段名」校验态，本地先落盘）
        item: dict = {"key": key, "template": host, "label": label, "type": ftype}
        if ftype in ("number", "meter"):
            if f.get("min") is not None:
                try:
                    item["min"] = float(f["min"])
                except (TypeError, ValueError):
                    pass
            if f.get("max") is not None:
                try:
                    item["max"] = float(f["max"])
                except (TypeError, ValueError):
                    pass
            if ftype == "meter":
                item.setdefault("min", 0)
                item.setdefault("max", 100)
        initial = f.get("initial")
        if ftype == "text" and isinstance(initial, str):
            item["initial"] = initial[:200]
        elif ftype in ("number", "meter") and isinstance(initial, (int, float)):
            item["initial"] = float(initial)
        elif ftype == "list" and isinstance(initial, list):
            item["initial"] = [str(x)[:60] for x in initial[:16]]
        hint = str(f.get("hint") or "").strip()
        if hint:
            item["hint"] = hint[:80]
        fields.append(item)
        seen_keys.add(key)

    options = None
    if "options" in templates:
        opt_raw = raw.get("options") if isinstance(raw.get("options"), dict) else {}
        options = {}
        if opt_raw.get("count") not in (None, ""):
            count = _int_field(opt_raw.get("count"), 3)
            count = max(2, min(4, count))
            options["count"] = count
        hint = str(opt_raw.get("hint") or "").strip()[:120]
        if hint:
            options["hint"] = hint
        # 允许空对象 = 官网「自动（2~4 个）」且无额外提示
        if not options:
            options = {}

    if not templates and not panels:
        return None

    out: dict = {
        "version": 1,
        "templates": templates,
        "panels": panels,
        "fields": fields,
        "options": options,
        "theme": raw.get("theme") if raw.get("theme") is not None else None,
    }
    return out


def _load_extensions(d: Path, fallback: dict | None = None) -> dict:
    ext = _read_json(d / "extensions.json", None)
    if not isinstance(ext, dict):
        ext = {}
    if fallback and isinstance(fallback, dict):
        for k, v in fallback.items():
            if k not in ext or ext.get(k) in (None, "", {}, []):
                ext[k] = v
    st = normalize_status_template(ext.get("status_template"))
    if st:
        ext["status_template"] = st
    elif "status_template" in ext:
        ext = dict(ext)
        ext.pop("status_template", None)
    return ext


def _write_extensions(d: Path, extensions) -> dict:
    ext = dict(extensions) if isinstance(extensions, dict) else {}
    st = normalize_status_template(ext.get("status_template"))
    if st:
        ext["status_template"] = st
    else:
        ext.pop("status_template", None)
    # 空对象也落盘，方便确认已同步
    _write_json(d / "extensions.json", ext)
    return ext


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _card_dir(local_id: str) -> Path:
    local_id = _safe_folder_name(local_id)
    if not local_id or local_id in (".", "..") or "/" in local_id or "\\" in local_id:
        raise ValueError("无效卡名/文件夹")
    if local_id.startswith(".") or local_id.startswith("_"):
        raise ValueError("卡名不能以 . 或 _ 开头（保留给模板/系统目录）")
    return cards_root() / local_id


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text or "").replace("\r\n", "\n").rstrip() + "\n", encoding="utf-8")


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _tags_from_text(text: str) -> list[str]:
    parts = re.split(r"[,，\n]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def _tags_to_text(tags) -> str:
    if isinstance(tags, list):
        return ", ".join(str(t).strip() for t in tags if str(t).strip())
    return str(tags or "").strip()


def _bool_text(v) -> str:
    return "true" if v else "false"


def _parse_bool(text: str, default: bool = True) -> bool:
    s = (text or "").strip().lower()
    if s in ("1", "true", "yes", "y", "是", "启用"):
        return True
    if s in ("0", "false", "no", "n", "否", "禁用"):
        return False
    return default


def folder_mtime(local_id: str) -> float:
    d = _card_dir(local_id)
    if not d.is_dir():
        return 0.0
    latest = d.stat().st_mtime
    for p in d.rglob("*"):
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            continue
    return latest


def folder_fingerprint(local_id: str) -> str:
    """Detect disk edits even when mtime resolution is coarse (size + mtime_ns)."""
    d = _card_dir(local_id)
    if not d.is_dir():
        return ""
    parts: list[str] = []
    try:
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in (".txt", ".json", ".png", ".jpg", ".jpeg", ".webp", ".gif"):
                continue
            try:
                st = p.stat()
                rel = p.relative_to(d).as_posix()
                ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
                parts.append(f"{rel}:{ns}:{st.st_size}")
            except OSError:
                continue
    except OSError:
        return ""
    return hashlib.md5("|".join(parts).encode("utf-8", "replace")).hexdigest()


def _load_world_book(d: Path) -> dict:
    wb_dir = d / "character_book"
    book = {"name": "世界设定", "entries": [], "extensions": {}}
    # 兼容：根级 character_book.json
    legacy = _read_json(d / "character_book.json", None)
    if isinstance(legacy, dict) and isinstance(legacy.get("entries"), list):
        book = {
            "name": str(legacy.get("name") or "世界设定"),
            "entries": legacy.get("entries") or [],
            "extensions": legacy.get("extensions") if isinstance(legacy.get("extensions"), dict) else {},
        }
    if wb_dir.is_dir():
        name = _read_text(wb_dir / "name.txt") or book["name"]
        book["name"] = name
        entries_dir = wb_dir / "entries"
        entries = []
        if entries_dir.is_dir():
            subdirs = sorted([p for p in entries_dir.iterdir() if p.is_dir()], key=lambda p: p.name)
            for i, ed in enumerate(subdirs, start=1):
                meta = _read_json(ed / "entry.json", {})
                if not isinstance(meta, dict):
                    meta = {}
                eid = meta.get("id")
                try:
                    eid = int(eid) if eid is not None else i
                except Exception:
                    eid = i
                keys_text = _read_text(ed / "keys.txt")
                keys = meta.get("keys") if isinstance(meta.get("keys"), list) else _tags_from_text(keys_text)
                content = _read_text(ed / "content.txt") or str(meta.get("content") or "")
                ename = _read_text(ed / "name.txt") or str(meta.get("name") or ed.name)
                enabled = _parse_bool(_read_text(ed / "enabled.txt"), bool(meta.get("enabled", True)))
                constant = _parse_bool(_read_text(ed / "constant.txt"), bool(meta.get("constant", False)))
                order_txt = _read_text(ed / "insertion_order.txt").strip()
                if order_txt != "":
                    insertion_order = _int_field(order_txt, i - 1)
                elif meta.get("insertion_order") is not None and meta.get("insertion_order") != "":
                    insertion_order = _int_field(meta.get("insertion_order"), i - 1)
                else:
                    insertion_order = i - 1
                pos_txt = _read_text(ed / "position.txt").strip()
                if pos_txt != "":
                    position = _int_field(pos_txt, 4)
                elif meta.get("position") is not None and meta.get("position") != "":
                    position = _int_field(meta.get("position"), 4)
                else:
                    position = 4
                pri_txt = _read_text(ed / "priority.txt").strip()
                if pri_txt != "":
                    priority = _int_field(pri_txt, 100)
                elif meta.get("priority") is not None and meta.get("priority") != "":
                    priority = _int_field(meta.get("priority"), 100)
                else:
                    priority = 100
                comment = _read_text(ed / "comment.txt") or str((meta.get("extensions") or {}).get("comment") or "")
                extensions = meta.get("extensions") if isinstance(meta.get("extensions"), dict) else {}
                if comment:
                    extensions = dict(extensions)
                    extensions["comment"] = comment
                entries.append(
                    {
                        "id": eid,
                        "name": ename,
                        "keys": keys,
                        "content": content,
                        "enabled": enabled,
                        "constant": constant,
                        "insertion_order": insertion_order,
                        "position": position,
                        "priority": priority,
                        "extensions": extensions,
                    }
                )
        if entries:
            book["entries"] = entries
        elif isinstance(legacy, dict) and legacy.get("entries"):
            pass
    return book


def _write_world_book(d: Path, book: dict) -> None:
    if not isinstance(book, dict):
        book = {"name": "世界设定", "entries": [], "extensions": {}}
    wb_dir = d / "character_book"
    entries = book.get("entries") if isinstance(book.get("entries"), list) else []
    _write_text(wb_dir / "name.txt", str(book.get("name") or "世界设定"))
    entries_dir = wb_dir / "entries"
    if entries_dir.exists():
        for old in entries_dir.iterdir():
            if old.is_dir():
                for f in old.iterdir():
                    try:
                        f.unlink()
                    except OSError:
                        pass
                try:
                    old.rmdir()
                except OSError:
                    pass
    entries_dir.mkdir(parents=True, exist_ok=True)
    for i, ent in enumerate(entries, start=1):
        if not isinstance(ent, dict):
            continue
        folder = entries_dir / f"{i:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        keys = ent.get("keys") if isinstance(ent.get("keys"), list) else []
        _write_text(folder / "name.txt", str(ent.get("name") or f"条目{i}"))
        _write_text(folder / "keys.txt", ", ".join(str(k) for k in keys))
        _write_text(folder / "content.txt", str(ent.get("content") or ""))
        _write_text(folder / "enabled.txt", _bool_text(bool(ent.get("enabled", True))))
        _write_text(folder / "constant.txt", _bool_text(bool(ent.get("constant", False))))
        insertion_order = _int_field(ent.get("insertion_order"), i - 1)
        position = _int_field(ent.get("position"), 4)
        priority = _int_field(ent.get("priority"), 100)
        eid = _int_field(ent.get("id"), i)
        _write_text(folder / "insertion_order.txt", str(insertion_order))
        _write_text(folder / "position.txt", str(position))
        _write_text(folder / "priority.txt", str(priority))
        ext = ent.get("extensions") if isinstance(ent.get("extensions"), dict) else {}
        comment = str(ext.get("comment") or "")
        _write_text(folder / "comment.txt", comment)
        _write_json(
            folder / "entry.json",
            {
                "id": eid,
                "name": str(ent.get("name") or f"条目{i}"),
                "keys": keys,
                "content": str(ent.get("content") or ""),
                "enabled": bool(ent.get("enabled", True)),
                "constant": bool(ent.get("constant", False)),
                "insertion_order": insertion_order,
                "position": position,
                "priority": priority,
                "extensions": ext,
            },
        )
    _write_json(
        d / "character_book.json",
        {
            "name": str(book.get("name") or "世界设定"),
            "entries": entries,
            "extensions": book.get("extensions") if isinstance(book.get("extensions"), dict) else {},
        },
    )


def _load_chat_history(d: Path, data: dict) -> list:
    ch = _read_json(d / "chat_history.json", None)
    if isinstance(ch, list):
        return ch
    # 若只有 first_mes，合成一段开场对话
    first = str(data.get("first_mes") or "").strip()
    if first:
        return [{"id": "1", "name": "开场对话", "messages": [{"role": "assistant", "content": first}]}]
    return data.get("chat_history") if isinstance(data.get("chat_history"), list) else []


def load_from_folder(local_id: str) -> dict:
    d = _card_dir(local_id)
    if not d.is_dir():
        raise FileNotFoundError(f"本地卡夹不存在: {local_id}")
    fields = {k: _read_text(d / f"{k}.txt") for k in BASIC_TXT}
    name = fields["name"].strip() or d.name
    card = empty_card(name)
    data = card["data"]
    data["name"] = name
    data["description"] = fields["description"]
    data["personality"] = fields["personality"]
    data["scenario"] = fields["scenario"]
    data["system_prompt"] = fields["system_prompt"]
    data["creator_notes"] = fields["creator_notes"]
    data["tags"] = _tags_from_text(fields["tags"])
    data["creator"] = fields["creator"]
    data["character_version"] = fields["character_version"]
    data["first_mes"] = fields["first_mes"]
    data["avatar_url"] = fields["avatar_url"]
    data["character_book"] = _load_world_book(d)
    data["chat_history"] = _load_chat_history(d, data)
    data["image_info"] = _read_json(d / "image_info.json", [])
    if not isinstance(data["image_info"], list):
        data["image_info"] = []
    data["voice_settings"] = _read_json(d / "voice_settings.json", None)
    replies = _read_text(d / "suggested_replies.txt")
    data["suggested_replies"] = [ln.strip() for ln in replies.splitlines() if ln.strip()] if replies else []
    alt = _read_json(d / "alternate_greetings.json", [])
    data["alternate_greetings"] = alt if isinstance(alt, list) else []
    # 兼容旧 card.json：补空字段，并保留云端元数据（cloudId / 上架状态）
    jp = d / "card.json"
    old_meta = {}
    od_extensions = None
    if jp.is_file():
        try:
            old = json.loads(jp.read_text(encoding="utf-8"))
            od = old.get("data") if isinstance(old, dict) else None
            if isinstance(od, dict):
                for k in (
                    "description",
                    "personality",
                    "scenario",
                    "system_prompt",
                    "creator_notes",
                    "first_mes",
                    "creator",
                    "character_version",
                    "avatar_url",
                ):
                    if not data.get(k) and od.get(k):
                        data[k] = od[k]
                if not data["tags"] and od.get("tags"):
                    data["tags"] = od["tags"]
                if not data["character_book"].get("entries") and isinstance(od.get("character_book"), dict):
                    data["character_book"] = od["character_book"]
                if not data["chat_history"] and isinstance(od.get("chat_history"), list):
                    data["chat_history"] = od["chat_history"]
                if od.get("db_id") not in (None, "", -1, "-1") and data.get("db_id") in (None, "", -1, "-1"):
                    data["db_id"] = od.get("db_id")
                if isinstance(od.get("extensions"), dict):
                    od_extensions = od["extensions"]
            if isinstance(old, dict) and isinstance(old.get("_meta"), dict):
                old_meta = dict(old["_meta"])
        except Exception:
            pass
    data["extensions"] = _load_extensions(d, od_extensions)
    # 若 chat 有首条 AI 而 first_mes 空，回填
    if not data["first_mes"] and data["chat_history"]:
        try:
            msgs = data["chat_history"][0].get("messages") or []
            for m in msgs:
                if isinstance(m, dict) and m.get("role") in ("assistant", "ai") and m.get("content"):
                    data["first_mes"] = str(m["content"])
                    break
        except Exception:
            pass
    meta = card.get("_meta") if isinstance(card.get("_meta"), dict) else {}
    for k, v in old_meta.items():
        if k in ("localId", "folder", "brief", "updatedAt", "mtime", "source"):
            continue
        if v is not None and v != "":
            meta[k] = v
    meta["localId"] = d.name
    meta["folder"] = str(d)
    meta["brief"] = fields["brief"]
    meta["updatedAt"] = str(old_meta.get("updatedAt") or _now_iso())
    meta["mtime"] = folder_mtime(d.name)
    meta["source"] = str(old_meta.get("source") or meta.get("source") or "local-folder")
    card["_meta"] = meta
    return card


def _card_content_score(data: dict | None) -> int:
    """粗估卡内容量，用于防止空表单误覆盖已有正文。"""
    if not isinstance(data, dict):
        return 0
    score = 0
    for k in (
        "description",
        "personality",
        "scenario",
        "system_prompt",
        "creator_notes",
        "first_mes",
    ):
        score += len(str(data.get(k) or "").strip())
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    score += sum(len(str(t).strip()) for t in tags)
    book = data.get("character_book") if isinstance(data.get("character_book"), dict) else {}
    entries = book.get("entries") if isinstance(book.get("entries"), list) else []
    for ent in entries:
        if isinstance(ent, dict):
            score += len(str(ent.get("content") or "").strip())
    chat = data.get("chat_history") if isinstance(data.get("chat_history"), list) else []
    for seg in chat:
        if not isinstance(seg, dict):
            continue
        for msg in seg.get("messages") or []:
            if isinstance(msg, dict):
                score += len(str(msg.get("content") or "").strip())
    if str(data.get("avatar_url") or "").strip():
        score += 20
    images = data.get("image_info") if isinstance(data.get("image_info"), list) else []
    score += 10 * len(images)
    return score


def write_folder(card: dict, local_id: str | None = None, *, brief: str | None = None) -> dict:
    if not isinstance(card, dict) or not isinstance(card.get("data"), dict):
        raise ValueError("卡必须是对象")
    data = card["data"]
    # 允许显式空名称（空白新建）；文件夹名仍由 local_id 决定
    if data.get("name") is None and local_id:
        name = _safe_folder_name(local_id)
    else:
        name = str(data.get("name") or "").strip()
    data["name"] = name
    folder_name = _safe_folder_name(local_id or name or "未命名角色")
    d = _card_dir(folder_name)
    d.mkdir(parents=True, exist_ok=True)

    # 拒绝用近乎空白的卡覆盖已有丰满内容（常见于空表单误点保存）
    new_score = _card_content_score(data)
    if (d / "card.json").is_file() or (d / "description.txt").is_file():
        try:
            existing = load_from_folder(folder_name)
            old_score = _card_content_score(existing.get("data"))
        except Exception:
            old_score = 0
        if old_score >= 80 and new_score < max(40, old_score // 5):
            raise ValueError(
                f"拒绝覆盖：本地卡「{folder_name}」已有内容（约 {old_score}），"
                f"当前提交几乎为空（约 {new_score}）。请先点「重载当前」再保存。"
            )

    meta = card.get("_meta") if isinstance(card.get("_meta"), dict) else {}
    if brief is None:
        brief = str(meta.get("brief") or "")

    _write_text(d / "name.txt", name)
    _write_text(d / "description.txt", str(data.get("description") or ""))
    _write_text(d / "personality.txt", str(data.get("personality") or ""))
    _write_text(d / "scenario.txt", str(data.get("scenario") or ""))
    _write_text(d / "system_prompt.txt", str(data.get("system_prompt") or ""))
    _write_text(d / "creator_notes.txt", str(data.get("creator_notes") or ""))
    _write_text(d / "tags.txt", _tags_to_text(data.get("tags")))
    _write_text(d / "creator.txt", str(data.get("creator") or ""))
    _write_text(d / "character_version.txt", str(data.get("character_version") or ""))
    _write_text(d / "first_mes.txt", str(data.get("first_mes") or ""))
    _write_text(d / "brief.txt", str(brief or ""))
    _write_text(d / "avatar_url.txt", str(data.get("avatar_url") or ""))

    replies = data.get("suggested_replies") if isinstance(data.get("suggested_replies"), list) else []
    _write_text(d / "suggested_replies.txt", "\n".join(str(x) for x in replies if str(x).strip()))
    _write_json(d / "alternate_greetings.json", data.get("alternate_greetings") if isinstance(data.get("alternate_greetings"), list) else [])
    _write_json(d / "image_info.json", data.get("image_info") if isinstance(data.get("image_info"), list) else [])
    _write_json(d / "voice_settings.json", data.get("voice_settings"))
    chat = data.get("chat_history") if isinstance(data.get("chat_history"), list) else []
    # 若无对话但有 first_mes，落成一段开场
    if not chat and str(data.get("first_mes") or "").strip():
        chat = [{
            "id": "1",
            "name": "开场对话",
            "messages": [{"role": "assistant", "content": str(data.get("first_mes"))}],
        }]
        data["chat_history"] = chat
    _write_json(d / "chat_history.json", chat)
    _write_world_book(d, data.get("character_book") if isinstance(data.get("character_book"), dict) else {})
    data["extensions"] = _write_extensions(d, data.get("extensions"))

    card.setdefault("spec", "chara_card_v3")
    card.setdefault("spec_version", "3.0")
    card["data"] = data
    meta["localId"] = folder_name
    meta["folder"] = str(d)
    meta["updatedAt"] = _now_iso()
    meta["source"] = "local-folder"
    meta["brief"] = str(brief or "")
    meta["mtime"] = folder_mtime(folder_name)
    card["_meta"] = meta
    _write_json(d / "card.json", card)

    readme = d / "README.md"
    if not readme.exists():
        readme.write_text(
            "# 本地角色卡\n\n"
            "写卡规范与顺序（无记忆时必读）：仓库根目录 `_模板/`\n\n"
            "- Agent 入口：`_模板/AGENTS.md`\n"
            "- 填写顺序：`_模板/填写顺序.md`\n"
            "- 写作规范：`_模板/写作规范.md`\n\n"
            "## 本夹文件\n\n"
            "## 基础 basic\n"
            "- `name.txt` `description.txt` `personality.txt` `scenario.txt`\n"
            "- `system_prompt.txt` `creator_notes.txt` `tags.txt` "
            "`creator.txt` `character_version.txt`\n"
            "- `first_mes.txt` `brief.txt` `avatar_url.txt`\n\n"
            "## 互动模板\n"
            "- `extensions.json`（含 `status_template` / `recommended_model`）\n\n"
            "## 世界书 worldbook\n"
            "- `character_book/name.txt`\n"
            "- `character_book/entries/001/`：`name.txt` `keys.txt` `content.txt` "
            "`enabled.txt` `constant.txt` `comment.txt` …\n\n"
            "## 对话 dialogue\n"
            "- `chat_history.json`\n"
            "- `suggested_replies.txt`（一行一条）\n\n"
            "## 图片 / 音色\n"
            "- `image_info.json` `voice_settings.json`\n\n"
            "编辑本目录文件后，控制台网页会实时同步。\n",
            encoding="utf-8",
        )
    return {"localId": folder_name, "path": str(d), "card": card, "mtime": meta["mtime"], "folder": str(d)}


def _unique_local_id(base: str) -> str:
    """避免新建覆盖已有卡夹：未命名角色 → 未命名角色-2 …"""
    base = _safe_folder_name(base)
    if not _card_dir(base).exists():
        return base
    for i in range(2, 1000):
        cand = _safe_folder_name(f"{base}-{i}")
        if not _card_dir(cand).exists():
            return cand
    raise RuntimeError(f"无法分配唯一卡夹名：{base}")


def create_local(name: str = "未命名角色", brief: str = "", *, blank: bool = False) -> dict:
    """
    新建本地卡夹。
    blank=True：内容字段留空（简述等），但必须带卡名；文件夹按卡名分配唯一目录。
    """
    display = (name or "").strip()
    if not display:
        raise ValueError("必须填写卡名")
    folder_base = _safe_folder_name(display)
    folder = _unique_local_id(folder_base)
    if blank:
        card = empty_card(display)
        card["data"]["name"] = display
        brief = ""
    else:
        card = empty_card(display)
        if brief:
            card["_meta"]["brief"] = brief
    return write_folder(card, local_id=folder, brief=str(brief or ""))


def list_local_cards() -> list[dict]:
    root = cards_root()
    items = []
    for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not _is_user_card_dir(path):
            continue
        try:
            card = load_from_folder(path.name)
        except Exception:
            continue
        d = card.get("data") or {}
        meta = card.get("_meta") or {}
        book = d.get("character_book") if isinstance(d.get("character_book"), dict) else {}
        entries = book.get("entries") if isinstance(book.get("entries"), list) else []
        cloud_id = meta.get("cloudId")
        draft_id = meta.get("draftId")
        try:
            db_id = int((d.get("db_id") if d else None) or 0)
        except (TypeError, ValueError):
            db_id = 0
        if (not cloud_id or int(cloud_id or 0) <= 0) and db_id > 0:
            cloud_id = db_id
        items.append(
            {
                "localId": path.name,
                "name": str(d.get("name") or path.name),
                "updatedAt": str(meta.get("updatedAt") or ""),
                "mtime": float(meta.get("mtime") or folder_mtime(path.name)),
                "folder": str(path),
                "worldBookEntries": len(entries),
                "cloudId": cloud_id,
                "draftId": draft_id,
                "published": bool(cloud_id and int(cloud_id or 0) > 0),
                "isListed": bool(meta.get("isListed")),
                "isPendingReview": bool(meta.get("isPendingReview")),
                "publishStatus": meta.get("publishStatus") or None,
                "path": str(path),
            }
        )
    return items


def load_local(local_id: str) -> dict:
    return load_from_folder(local_id)


def save_local(
    card: dict,
    local_id: str | None = None,
    *,
    previous_local_id: str | None = None,
    force: bool = False,
) -> dict:
    """写入卡夹。换目录名时若目标已存在且非原卡，默认拒绝覆盖。"""
    target = _safe_folder_name(local_id or "") if local_id else ""
    prev = _safe_folder_name(previous_local_id or "") if previous_local_id else ""
    if target and prev and target != prev and _card_dir(target).is_dir() and not force:
        raise ValueError(f"卡夹「{target}」已存在，请换名或先删除后再保存")
    return write_folder(card, local_id=local_id or None)


def prepare_workspace(name: str, brief: str = "") -> dict:
    name = _safe_folder_name(name or "未命名角色")
    d = _card_dir(name)
    if d.is_dir():
        card = load_from_folder(name)
        if brief and not (card.get("_meta") or {}).get("brief"):
            card["_meta"]["brief"] = brief
            return {**write_folder(card, local_id=name, brief=brief), "created": False}
        return {
            "localId": name,
            "path": str(d),
            "folder": str(d),
            "card": card,
            "mtime": folder_mtime(name),
            "created": False,
        }
    return {**create_local(name, brief=brief), "created": True}


def _decode_image_bytes(raw_b64: str = "", *, raw: bytes | None = None, filename: str = "", mime: str = "") -> tuple[bytes, str]:
    if raw is None:
        data = (raw_b64 or "").strip()
        if "," in data and data.lower().startswith("data:"):
            header, data = data.split(",", 1)
            if not mime and ";" in header:
                mime = header.split(";")[0].replace("data:", "")
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception as e:
            raise ValueError(f"Base64 无效: {e}") from e
    if not raw:
        raise ValueError("空文件")
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("图片太大（上限 12MB）")
    mime = (mime or "").split(";")[0].strip().lower()
    ext = _AVATAR_MIME.get(mime)
    if not ext:
        suffix = Path(filename or "").suffix.lower()
        if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg" if suffix == ".jpeg" else suffix
        elif raw[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            ext = ".webp"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            ext = ".gif"
        else:
            raise ValueError("仅支持 jpg/png/webp/gif")
    return raw, ext


def default_voice_settings(voice: dict | None = None) -> dict | None:
    if not voice:
        return None
    return {
        "voice": {
            "id": voice.get("id"),
            "name": voice.get("name") or "",
            "avatar_url": voice.get("avatar_url"),
            "preview_url": voice.get("preview_url"),
            "gender": voice.get("gender"),
        },
        "settings": {
            "ignore_parentheses": False,
            "only_quotes": False,
            "ignore_english": False,
            "read_asterisks": False,
        },
    }


def list_platform_voices() -> dict:
    """拉取平台公共音色 + 我的音色。"""
    public = _trpc_get("studio.getVoices", {})
    mine = _trpc_get("studio.getMyVoices", {})
    pub_items = public if isinstance(public, list) else (public.get("items") if isinstance(public, dict) else [])
    my_items = mine if isinstance(mine, list) else (mine.get("items") if isinstance(mine, dict) else [])
    if not isinstance(pub_items, list):
        pub_items = []
    if not isinstance(my_items, list):
        my_items = []

    def norm(it: dict, *, mine_flag: bool) -> dict:
        return {
            "id": it.get("id"),
            "name": it.get("name") or "",
            "gender": it.get("gender") or "",
            "isPublic": bool(it.get("is_public", not mine_flag)),
            "mine": mine_flag,
            "generationCount": it.get("generation_count") or 0,
            "previewUrl": it.get("preview_url") or "",
            "avatarUrl": it.get("avatar_url") or "",
            "description": it.get("voice_description") or it.get("description") or "",
        }

    return {
        "public": [norm(x, mine_flag=False) for x in pub_items if isinstance(x, dict)],
        "mine": [norm(x, mine_flag=True) for x in my_items if isinstance(x, dict)],
    }


def resolve_card_asset(local_id: str, rel: str) -> Path:
    """安全解析卡夹内资源路径（仅允许 assets/ 下）。"""
    d = _card_dir(local_id)
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel.startswith("assets/") or ".." in rel.split("/"):
        raise ValueError("仅允许读取 assets/ 下文件")
    path = (d / rel).resolve()
    assets = (d / "assets").resolve()
    if not str(path).startswith(str(assets)) or not path.is_file():
        raise FileNotFoundError("资源不存在")
    return path


def save_local_avatar(
    local_id: str,
    *,
    raw: bytes | None = None,
    data_b64: str = "",
    filename: str = "",
    mime: str = "",
) -> dict:
    """把本地图片写入 卡/<名>/assets/avatar.*，并更新 avatar_url / image_info[0]。"""
    local_id = _safe_folder_name(local_id)
    d = _card_dir(local_id)
    if not d.is_dir():
        create_local(local_id)
    raw, ext = _decode_image_bytes(data_b64, raw=raw, filename=filename, mime=mime)

    assets = d / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for old in assets.glob("avatar.*"):
        try:
            old.unlink()
        except OSError:
            pass
    rel = f"assets/avatar{ext}"
    out = d / rel
    out.write_bytes(raw)

    card = load_from_folder(local_id)
    card["data"]["avatar_url"] = rel
    images = card["data"].get("image_info") if isinstance(card["data"].get("image_info"), list) else []
    cover = {
        "url": rel,
        "name": "头像",
        "isHidden": False,
        "triggerKeywords": [],
        "local": True,
    }
    if images:
        images[0] = {**images[0], **cover}
    else:
        images = [cover]
    card["data"]["image_info"] = images
    saved = write_folder(card, local_id=local_id)
    return {
        **saved,
        "rel": rel,
        "file": str(out),
        "avatarUrl": rel,
        "serveUrl": f"/api/card/asset?id={urllib.parse.quote(local_id)}&path={urllib.parse.quote(rel)}",
    }


def save_local_avatar_b64(local_id: str, data_b64: str, *, filename: str = "", mime: str = "") -> dict:
    return save_local_avatar(local_id, data_b64=data_b64, filename=filename, mime=mime)


def save_local_image(
    local_id: str,
    *,
    data_b64: str = "",
    raw: bytes | None = None,
    filename: str = "",
    mime: str = "",
    name: str = "",
    set_avatar: bool = False,
) -> dict:
    """追加一张立绘到 image_info，文件落在 assets/image_NNN.*。"""
    local_id = _safe_folder_name(local_id)
    d = _card_dir(local_id)
    if not d.is_dir():
        create_local(local_id)
    raw, ext = _decode_image_bytes(data_b64, raw=raw, filename=filename, mime=mime)
    assets = d / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    # 下一个序号
    n = 1
    for p in assets.glob("image_*"):
        m = re.match(r"image_(\d+)", p.stem)
        if m:
            n = max(n, int(m.group(1)) + 1)
    rel = f"assets/image_{n:03d}{ext}"
    out = d / rel
    out.write_bytes(raw)

    card = load_from_folder(local_id)
    images = card["data"].get("image_info") if isinstance(card["data"].get("image_info"), list) else []
    item = {
        "url": rel,
        "name": (name or Path(filename).stem or f"立绘{n}").strip() or f"立绘{n}",
        "isHidden": False,
        "triggerKeywords": [],
        "local": True,
    }
    images.append(item)
    card["data"]["image_info"] = images
    if set_avatar or not str(card["data"].get("avatar_url") or "").strip():
        card["data"]["avatar_url"] = rel
    saved = write_folder(card, local_id=local_id)
    return {
        **saved,
        "rel": rel,
        "file": str(out),
        "serveUrl": f"/api/card/asset?id={urllib.parse.quote(local_id)}&path={urllib.parse.quote(rel)}",
        "index": len(images) - 1,
    }


def remove_local_image(local_id: str, index: int) -> dict:
    card = load_from_folder(local_id)
    images = card["data"].get("image_info") if isinstance(card["data"].get("image_info"), list) else []
    if index < 0 or index >= len(images):
        raise ValueError("立绘索引无效")
    item = images.pop(index)
    url = str((item or {}).get("url") or "")
    d = _card_dir(local_id)
    if url.startswith("assets/") and ".." not in url:
        try:
            p = (d / url).resolve()
            if str(p).startswith(str((d / "assets").resolve())) and p.is_file():
                # 不删 avatar.*（可能与头像共用）
                if not p.name.startswith("avatar."):
                    p.unlink()
        except OSError:
            pass
    card["data"]["image_info"] = images
    # 若删掉的是当前头像，回退到第一张
    if str(card["data"].get("avatar_url") or "") == url:
        card["data"]["avatar_url"] = (images[0].get("url") if images else "") or ""
    return write_folder(card, local_id=local_id)


def set_voice_settings(local_id: str, voice: dict | None, settings: dict | None = None) -> dict:
    card = load_from_folder(local_id)
    if voice is None:
        card["data"]["voice_settings"] = None
    else:
        vs = default_voice_settings(voice)
        if settings and isinstance(settings, dict) and vs:
            cur = vs.get("settings") or {}
            for k in ("ignore_parentheses", "only_quotes", "ignore_english", "read_asterisks"):
                if k in settings:
                    cur[k] = bool(settings[k])
            vs["settings"] = cur
        card["data"]["voice_settings"] = vs
    return write_folder(card, local_id=local_id)


def poll_local(local_id: str, since_mtime: float = 0.0, since_sig: str = "") -> dict:
    mtime = folder_mtime(local_id)
    sig = folder_fingerprint(local_id)
    since_m = float(since_mtime or 0)
    since_s = str(since_sig or "").strip()
    changed = (mtime > since_m) or (bool(sig) and sig != since_s) or since_m <= 0
    payload = {
        "ok": True,
        "localId": _safe_folder_name(local_id),
        "mtime": mtime,
        "sig": sig,
        "changed": changed,
        "folder": str(_card_dir(local_id)),
    }
    if changed:
        payload["card"] = load_from_folder(local_id)
    return payload


def _trpc_get(path: str, payload: dict | None = None) -> dict:
    cookie, token, _, _ = _require_auth(30)
    payload = payload or {}
    q = urllib.parse.quote(json.dumps({"0": {"json": payload}}, separators=(",", ":")))
    url = f"{studio.get_origin()}/api/trpc/{path}?batch=1&input={q}"
    st, raw, _ = studio.http(url, cookie, token, method="GET", timeout=45, accept="application/json")
    obj = json.loads(raw.decode("utf-8", "replace"))
    if st != 200:
        err = obj
        if isinstance(obj, list) and obj:
            err = (((obj[0] or {}).get("error") or {}).get("json") or {})
        raise RuntimeError(f"tRPC {path} HTTP {st}: {err}")
    if isinstance(obj, list) and obj:
        return (((obj[0] or {}).get("result") or {}).get("data") or {}).get("json") or {}
    return obj


def _normalize_cloud_item(it: dict) -> dict | None:
    if not isinstance(it, dict):
        return None
    raw = it.get("rawData") if isinstance(it.get("rawData"), dict) else {}
    if not raw and isinstance(it.get("raw_data"), dict):
        raw = it["raw_data"]
    d = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    is_draft = bool(it.get("isDraft"))
    db_id = d.get("db_id")
    try:
        db_id = int(db_id) if db_id is not None else -1
    except (TypeError, ValueError):
        db_id = -1
    item_id = it.get("id")
    try:
        item_id = int(item_id) if item_id is not None else 0
    except (TypeError, ValueError):
        item_id = 0
    # 已发布项：列表 id 即角色 id；草稿可能带着指向正式卡的 db_id
    character_id = item_id if not is_draft else (db_id if db_id > 0 else None)
    return {
        "cloudId": item_id,
        "dbId": db_id if db_id > 0 else (item_id if not is_draft and item_id > 0 else None),
        "characterId": character_id if character_id and character_id > 0 else None,
        "name": d.get("name") or it.get("name") or (f"草稿-{item_id}" if is_draft else str(item_id)),
        "isDraft": is_draft,
        "isGamefy": bool(it.get("isGamefy")),
        "createdAt": it.get("createdAt") or "",
        "updatedAt": it.get("updatedAt") or it.get("updated_at") or it.get("createdAt") or "",
        "isPublic": None,
        "isHidden": None,
        "publishStatus": None,
        "isListed": False,
        "isPendingReview": False,
        "firstPublishedAt": "",
    }


def _parse_flight_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    if raw in _FLIGHT_BOOL:
        return _FLIGHT_BOOL[raw]
    return None


def _parse_flight_str(raw: str | None) -> str | None:
    if raw is None or raw == "null" or raw == "void 0":
        return None
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


def fetch_character_listing_status(card_id: int) -> dict:
    """从角色页 SSR 读取广场状态：isPublic / publishStatus / isHidden。

    studio.getCharacters 不含这些字段；上架走 card.publish，过审后 isPublic=true。
    """
    card_id = int(card_id)
    if card_id <= 0:
        raise ValueError("需要有效 cardId")
    cookie, token, _, _ = _require_auth(30)
    st, raw, _ = studio.http(
        f"{studio.get_origin()}/character/{card_id}",
        cookie,
        token,
        method="GET",
        timeout=45,
        accept="text/html",
    )
    html = raw.decode("utf-8", "replace")
    if st != 200:
        raise RuntimeError(f"角色页 HTTP {st}")
    m = re.search(rf"(?:characterData:\$R\[\d+\]={{)?id:{card_id},", html)
    if not m:
        raise RuntimeError(f"角色页未找到 cardId={card_id} 的 characterData")
    chunk = html[m.start() : m.start() + 6000]

    def _field(key: str) -> str | None:
        mm = re.search(rf"{key}:(!0|!1|null|void 0|\"[^\"]*\"|[A-Za-z0-9_]+)", chunk)
        return mm.group(1) if mm else None

    is_public = _parse_flight_bool(_field("isPublic"))
    is_hidden = _parse_flight_bool(_field("isHidden"))
    publish_status = _parse_flight_str(_field("publishStatus")) or ""
    first_published = _parse_flight_str(_field("firstPublishedAt")) or ""
    is_pending = publish_status in _PENDING_STATUSES
    is_listed = bool(is_public) or (publish_status in _LISTED_STATUSES and not is_hidden)
    return {
        "cardId": card_id,
        "isPublic": is_public,
        "isHidden": is_hidden,
        "publishStatus": publish_status or None,
        "firstPublishedAt": first_published,
        "isPendingReview": is_pending,
        "isListed": bool(is_listed and not is_hidden),
        "listingStatusKnown": True,
    }


def _game_stats_public_map() -> dict[int, dict]:
    """游戏卡可从 getGameStats 拿到 isPublic（角色卡不在此列表）。"""
    out: dict[int, dict] = {}
    try:
        data = _trpc_get("studio.getGameStats", {})
    except Exception:
        return out
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list):
        return out
    for c in cards:
        if not isinstance(c, dict):
            continue
        try:
            cid = int(c.get("cardId") or c.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        is_public = bool(c.get("isPublic"))
        is_hidden = bool(c.get("isHidden"))
        out[cid] = {
            "isPublic": is_public,
            "isHidden": is_hidden,
            "publishStatus": "approved" if is_public else None,
            "firstPublishedAt": str(c.get("publishedAt") or ""),
            "isPendingReview": False,
            "isListed": bool(is_public and not is_hidden),
            "listingStatusKnown": True,
        }
    return out


def _apply_listing_fields(item: dict, status: dict | None) -> dict:
    if not status:
        item["isListed"] = False
        item["isPendingReview"] = False
        item["listingStatusKnown"] = False
        return item
    known = status.get("listingStatusKnown")
    if known is None:
        known = status.get("isPublic") is not None or bool(status.get("publishStatus"))
    item["listingStatusKnown"] = bool(known)
    item["isPublic"] = status.get("isPublic")
    item["isHidden"] = status.get("isHidden")
    item["publishStatus"] = status.get("publishStatus")
    item["firstPublishedAt"] = status.get("firstPublishedAt") or ""
    item["isPendingReview"] = bool(status.get("isPendingReview")) if known else False
    item["isListed"] = bool(status.get("isListed")) if known else False
    return item


def _patch_local_card_meta(local_name: str, meta_updates: dict, *, db_id: int | None = None) -> None:
    """只改 card.json 的 _meta（及可选 db_id），避免全量 write_folder 冲掉未保存 txt。"""
    d = _card_dir(local_name)
    jp = d / "card.json"
    payload: dict = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {}, "_meta": {}}
    if jp.is_file():
        try:
            old = json.loads(jp.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                payload.update({k: old[k] for k in ("spec", "spec_version") if k in old})
                if isinstance(old.get("data"), dict):
                    payload["data"] = dict(old["data"])
                if isinstance(old.get("_meta"), dict):
                    payload["_meta"] = dict(old["_meta"])
        except Exception:
            pass
    meta = payload["_meta"] if isinstance(payload.get("_meta"), dict) else {}
    meta.update({k: v for k, v in meta_updates.items() if v is not None})
    payload["_meta"] = meta
    if db_id is not None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        data["db_id"] = int(db_id)
        payload["data"] = data
    _write_json(jp, payload)


def _sync_local_listing_meta(cloud_id: int, *, status: dict, name_hint: str = "") -> None:
    """把云端上架状态写回本地卡夹 _meta。只按 cloudId/db_id 匹配，禁止按同名绑定。"""
    del name_hint  # 保留参数兼容旧调用；绝不用名字猜绑
    local_name = _find_local_id_by_cloud_id(cloud_id)
    if not local_name:
        return
    try:
        card = load_from_folder(local_name)
    except Exception:
        return
    meta = card.get("_meta") if isinstance(card.get("_meta"), dict) else {}
    data = card.get("data") if isinstance(card.get("data"), dict) else {}
    meta_updates: dict = {}
    db_patch: int | None = None
    if meta.get("cloudId") != cloud_id:
        meta_updates["cloudId"] = cloud_id
    try:
        db_id = int(data.get("db_id") or 0)
    except (TypeError, ValueError):
        db_id = 0
    if db_id != cloud_id:
        db_patch = cloud_id
    # 仅在读到明确 isPublic / publishStatus 时改上架标记
    definitive = status.get("isPublic") is not None or bool(status.get("publishStatus"))
    if definitive:
        listed = bool(status.get("isListed"))
        pending = bool(status.get("isPendingReview"))
        if bool(meta.get("isListed")) != listed:
            meta_updates["isListed"] = listed
        if listed and not meta.get("listedAt"):
            meta_updates["listedAt"] = status.get("firstPublishedAt") or _now_iso()
        pub_status = status.get("publishStatus")
        if meta.get("publishStatus") != pub_status:
            meta_updates["publishStatus"] = pub_status
        if bool(meta.get("isPendingReview")) != pending:
            meta_updates["isPendingReview"] = pending
    if not meta_updates and db_patch is None:
        return
    try:
        _patch_local_card_meta(local_name, meta_updates, db_id=db_patch)
    except Exception:
        return


def list_cloud_cards(*, enrich_listing: bool = True) -> list[dict]:
    """
    拉取创作侧全部角色/草稿。
    enrich_listing=False：只拉列表（供控制台轮询），不逐卡查广场上架状态。
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    cursor = None
    for _ in range(40):
        payload: dict = {}
        if cursor:
            payload["cursor"] = cursor
        data = _trpc_get("studio.getCharacters", payload)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            break
        for it in items:
            norm = _normalize_cloud_item(it)
            if not norm or not norm.get("cloudId"):
                continue
            key = (bool(norm.get("isDraft")), int(norm["cloudId"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(norm)
        if not data.get("hasMore"):
            break
        cursor = data.get("nextCursor")
        if not cursor:
            break

    if not enrich_listing:
        return out

    game_map = _game_stats_public_map()
    need_page: list[int] = []
    for it in out:
        if it.get("isDraft"):
            continue
        cid = int(it["cloudId"])
        if it.get("isGamefy") and cid in game_map:
            _apply_listing_fields(it, game_map[cid])
            continue
        need_page.append(cid)

    status_map: dict[int, dict] = {}
    if need_page:
        with ThreadPoolExecutor(max_workers=min(6, len(need_page))) as pool:
            futures = {pool.submit(fetch_character_listing_status, cid): cid for cid in need_page}
            for fut in as_completed(futures):
                cid = futures[fut]
                try:
                    status_map[cid] = fut.result()
                except Exception:
                    status_map[cid] = {
                        "cardId": cid,
                        "isPublic": None,
                        "isHidden": None,
                        "publishStatus": None,
                        "firstPublishedAt": "",
                        "isPendingReview": False,
                        "isListed": False,
                        "listingStatusKnown": False,
                    }

    for it in out:
        if it.get("isDraft"):
            continue
        cid = int(it["cloudId"])
        if not it.get("isListed") and not it.get("isPendingReview"):
            st = status_map.get(cid)
            if st:
                _apply_listing_fields(it, st)
            elif "listingStatusKnown" not in it:
                it["listingStatusKnown"] = False
        _sync_local_listing_meta(
            cid,
            status={
                "isListed": bool(it.get("isListed")),
                "isPendingReview": bool(it.get("isPendingReview")),
                "publishStatus": it.get("publishStatus"),
                "firstPublishedAt": it.get("firstPublishedAt") or "",
                "isPublic": it.get("isPublic"),
                "listingStatusKnown": bool(it.get("listingStatusKnown")),
            },
        )
    return out


def delete_local_card(local_id: str) -> dict:
    """删除本地卡夹目录。"""
    local_id = _safe_folder_name(local_id)
    d = _card_dir(local_id)
    if not d.is_dir():
        raise FileNotFoundError(f"本地卡夹不存在: {local_id}")
    shutil.rmtree(d)
    return {"localId": local_id, "deleted": True, "path": str(d)}


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _png_embed_text_chunks(png_bytes: bytes, texts: dict[str, str]) -> bytes:
    """在 IEND 前写入/替换 tEXt 块（SillyTavern：chara / ccv3）。"""
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("封面不是有效 PNG")
    out = bytearray(png_bytes[:8])
    pos = 8
    drop = {k.encode("latin-1") for k in texts}
    while pos + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[pos : pos + 4])[0]
        tag = png_bytes[pos + 4 : pos + 8]
        data = png_bytes[pos + 8 : pos + 8 + length]
        nxt = pos + 12 + length
        if tag == b"IEND":
            for key, val in texts.items():
                payload = key.encode("latin-1") + b"\x00" + val.encode("latin-1", "ignore")
                out.extend(_png_chunk(b"tEXt", payload))
            out.extend(png_bytes[pos:nxt])
            break
        if tag == b"tEXt":
            nul = data.find(b"\x00")
            key = data[:nul] if nul >= 0 else data
            if key in drop:
                pos = nxt
                continue
        out.extend(png_bytes[pos:nxt])
        pos = nxt
    else:
        raise ValueError("PNG 缺少 IEND")
    return bytes(out)


def _cover_url_candidates(card: dict) -> list[str]:
    data = card.get("data") if isinstance(card.get("data"), dict) else {}
    urls: list[str] = []
    images = data.get("image_info") if isinstance(data.get("image_info"), list) else []
    for it in images:
        if isinstance(it, dict):
            u = str(it.get("url") or "").strip()
            if u:
                urls.append(u)
    av = str(data.get("avatar_url") or "").strip()
    if av:
        urls.append(av)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _load_cover_bytes(local_id: str, url: str) -> bytes:
    url = (url or "").strip()
    if not url:
        raise ValueError("空封面地址")
    d = _card_dir(local_id)
    if _is_local_asset_url(url):
        p = resolve_card_asset(local_id, url)
        return p.read_bytes()
    # 相对 assets
    if not url.startswith("http://") and not url.startswith("https://"):
        rel = url.replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            raise ValueError("非法本地路径")
        p = (d / rel).resolve()
        if not str(p).startswith(str(d.resolve())) or not p.is_file():
            raise FileNotFoundError(f"封面不存在: {url}")
        return p.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "dzmm-local-dev/card-export"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _bytes_to_cover_png(raw: bytes, *, max_side: int = 1024) -> bytes:
    if Image is None:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return raw
        raise RuntimeError("需要 Pillow 才能把非 PNG 封面转成卡图")
    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGBA")
    w, h = im.size
    scale = min(1.0, float(max_side) / float(max(w, h) or 1))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _placeholder_cover_png(title: str = "card") -> bytes:
    if Image is None:
        # 1x1 PNG
        return (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00"))
            + _png_chunk(b"IEND", b"")
        )
    im = Image.new("RGB", (768, 1024), (232, 224, 214))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def export_card_png(local_id: str, *, save_copy: bool = True) -> dict:
    """
    打包为 SillyTavern 兼容 PNG 角色卡：
    封面优先 image_info 第一张，其次 avatar_url；写入 tEXt chara + ccv3。
    """
    local_id = _safe_folder_name(local_id)
    card = load_from_folder(local_id)
    data = card.get("data") if isinstance(card.get("data"), dict) else {}
    export_card = {
        "spec": card.get("spec") or "chara_card_v3",
        "spec_version": card.get("spec_version") or "3.0",
        "data": json.loads(json.dumps(data)),
    }
    # 导出不带本地 _meta
    payload = json.dumps(export_card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(payload).decode("ascii")

    cover_raw = None
    cover_src = ""
    for u in _cover_url_candidates(card):
        try:
            cover_raw = _load_cover_bytes(local_id, u)
            cover_src = u
            break
        except Exception:
            continue
    if cover_raw is None:
        cover_png = _placeholder_cover_png(str(data.get("name") or local_id))
        cover_src = "(placeholder)"
    else:
        cover_png = _bytes_to_cover_png(cover_raw)

    png = _png_embed_text_chunks(cover_png, {"ccv3": b64, "chara": b64})
    safe_name = _safe_folder_name(str(data.get("name") or local_id))
    filename = f"{safe_name}.png"
    out_path = None
    if save_copy:
        export_dir = _card_dir(local_id) / "export"
        export_dir.mkdir(parents=True, exist_ok=True)
        out_path = export_dir / filename
        out_path.write_bytes(png)
    return {
        "localId": local_id,
        "filename": filename,
        "bytes": png,
        "size": len(png),
        "coverSrc": cover_src,
        "path": str(out_path) if out_path else "",
    }


def delete_cloud_draft(draft_id: int) -> dict:
    """删除云端草稿 studio.deleteDraft。"""
    draft_id = int(draft_id)
    if draft_id <= 0:
        raise ValueError("无效草稿 id")
    result = _trpc_post("studio.deleteDraft", {"id": draft_id})
    return {"draftId": draft_id, "deleted": True, "result": result}


def hide_cloud_character(cloud_id: int) -> dict:
    """隐藏/下线已发布角色卡 studio.hideCharacterCard（公开页会 404）。"""
    cloud_id = int(cloud_id)
    if cloud_id <= 0:
        raise ValueError("无效 cloudId")
    result = _trpc_post("studio.hideCharacterCard", {"id": cloud_id})
    return {"cloudId": cloud_id, "hidden": True, "result": result}


def _delete_drafts_for_character(character_id: int, *, except_draft_id: int | None = None) -> list[dict]:
    """删除指向同一正式卡的全部草稿。"""
    character_id = int(character_id)
    removed = []
    for it in list_cloud_cards(enrich_listing=False):
        if not it.get("isDraft"):
            continue
        draft_id = int(it.get("cloudId") or 0)
        if draft_id <= 0:
            continue
        if except_draft_id and draft_id == int(except_draft_id):
            continue
        linked = it.get("characterId") or it.get("dbId")
        try:
            linked = int(linked) if linked is not None else 0
        except (TypeError, ValueError):
            linked = 0
        if linked != character_id:
            continue
        try:
            removed.append(delete_cloud_draft(draft_id))
        except Exception as e:
            removed.append({"draftId": draft_id, "deleted": False, "error": str(e)})
    return removed


def remove_cloud_card(
    *,
    cloud_id: int,
    is_draft: bool | None = None,
    also_hide_published: bool = False,
    cascade_drafts: bool = True,
    character_id: int | None = None,
) -> dict:
    """
    统一删除/下线：
    - 草稿：deleteDraft；若 also_hide_published 且能解析正式卡 id，再 hideCharacterCard
    - 已发布：hideCharacterCard；若 cascade_drafts，顺带删同卡草稿
    """
    cloud_id = int(cloud_id)
    if cloud_id <= 0:
        raise ValueError("无效 cloudId")

    # 未标明时按当前列表推断
    matched = None
    if is_draft is None or character_id is None:
        for it in list_cloud_cards():
            if int(it.get("cloudId") or 0) == cloud_id:
                matched = it
                break
    if is_draft is None:
        is_draft = bool(matched.get("isDraft")) if matched else None
    if character_id is None and matched:
        character_id = matched.get("characterId") or matched.get("dbId")
    try:
        character_id = int(character_id) if character_id is not None else 0
    except (TypeError, ValueError):
        character_id = 0

    actions: list[dict] = []

    if is_draft is True:
        actions.append({"op": "deleteDraft", **delete_cloud_draft(cloud_id)})
        pub_id = character_id if character_id > 0 else 0
        if also_hide_published and pub_id > 0:
            try:
                actions.append({"op": "hide", **hide_cloud_character(pub_id)})
            except Exception as e:
                actions.append({"op": "hide", "cloudId": pub_id, "hidden": False, "error": str(e)})
            if cascade_drafts:
                for row in _delete_drafts_for_character(pub_id, except_draft_id=cloud_id):
                    actions.append({"op": "deleteDraft", **row})
        return {
            "mode": "draft",
            "cloudId": cloud_id,
            "characterId": pub_id or None,
            "actions": actions,
        }

    # 已发布，或未标明：先 hide，失败再当草稿删
    if is_draft is False or is_draft is None:
        pub_id = character_id if character_id > 0 else cloud_id
        try:
            actions.append({"op": "hide", **hide_cloud_character(pub_id)})
            if cascade_drafts:
                for row in _delete_drafts_for_character(pub_id):
                    actions.append({"op": "deleteDraft", **row})
            return {
                "mode": "hide",
                "cloudId": pub_id,
                "characterId": pub_id,
                "actions": actions,
            }
        except Exception as hide_err:
            if is_draft is False:
                raise
            # 未标明且 hide 失败 → 尝试草稿
            try:
                actions.append({"op": "deleteDraft", **delete_cloud_draft(cloud_id)})
                return {
                    "mode": "draft",
                    "cloudId": cloud_id,
                    "characterId": None,
                    "actions": actions,
                    "note": f"hide failed: {hide_err}",
                }
            except Exception:
                raise hide_err from None

    raise ValueError("无法判断是草稿还是已发布卡")


def pull_cloud_card(cloud_id: int, *, folder_name: str | None = None, is_draft: bool | None = None) -> dict:
    cloud_id = int(cloud_id)
    # 未指定时先按正式卡取，失败再试草稿
    data = None
    draft = False
    if is_draft is True:
        data = _trpc_get("studio.getDraft", {"id": cloud_id})
        draft = True
    elif is_draft is False:
        data = _trpc_get("studio.getCharacterCard", {"id": cloud_id})
    else:
        try:
            data = _trpc_get("studio.getCharacterCard", {"id": cloud_id})
        except Exception:
            data = _trpc_get("studio.getDraft", {"id": cloud_id})
            draft = True

    raw = None
    if isinstance(data, dict):
        raw = data.get("rawData") if isinstance(data.get("rawData"), dict) else None
        if not raw and isinstance(data.get("raw_data"), dict):
            raw = data["raw_data"]
        if not raw and isinstance(data.get("data"), dict) and ("name" in data["data"] or "character_book" in data["data"]):
            # getDraft 有时直接给 data
            if data.get("spec") or "name" in (data.get("data") or {}):
                if "spec" in data:
                    raw = {"spec": data.get("spec"), "spec_version": data.get("spec_version") or "3.0", "data": data["data"]}
                else:
                    raw = {
                        "spec": "chara_card_v3",
                        "spec_version": "3.0",
                        "data": data["data"],
                    }
    if not isinstance(raw, dict):
        raise RuntimeError("云端卡缺少 rawData")
    card = json.loads(json.dumps(raw))
    if not isinstance(card.get("data"), dict):
        raise RuntimeError("云端卡 rawData 无效")
    # 游戏卡禁止拉到本地写卡目录
    is_gamefy = bool(data.get("isGamefy")) if isinstance(data, dict) else False
    if not is_gamefy:
        for it in list_cloud_cards():
            if int(it.get("cloudId") or 0) == cloud_id and not it.get("isDraft"):
                is_gamefy = bool(it.get("isGamefy"))
                break
    gamefy = (card.get("data") or {}).get("gamefy")
    if is_gamefy or (isinstance(gamefy, dict) and gamefy.get("id")):
        raise ValueError("游戏卡不能拉到本地写卡目录，请用游戏卡/本地桥工作流")
    name_src = (
        (folder_name or "").strip()
        or str(card["data"].get("name") or "").strip()
        or str(data.get("name") or "").strip()
        or (f"草稿-{cloud_id}" if draft else f"云端-{cloud_id}")
    )
    name = _safe_folder_name(name_src)
    # 同名夹若已绑定其他云端卡，改落到唯一目录，禁止静默覆盖
    existing_dir = _card_dir(name)
    if existing_dir.is_dir():
        try:
            existing = load_from_folder(name)
            ex_meta = existing.get("_meta") if isinstance(existing.get("_meta"), dict) else {}
            ex_cloud = int(ex_meta.get("cloudId") or 0)
            ex_draft = int(ex_meta.get("draftId") or 0)
            try:
                ex_db = int((existing.get("data") or {}).get("db_id") or 0)
            except (TypeError, ValueError):
                ex_db = 0
            same = (
                (not draft and (ex_cloud == cloud_id or ex_db == cloud_id))
                or (draft and ex_draft == cloud_id)
            )
            if not same and (ex_cloud or ex_draft or ex_db > 0 or _card_content_score(existing.get("data")) > 0):
                name = _unique_local_id(name)
        except Exception:
            name = _unique_local_id(name)
    card["data"]["name"] = str(card["data"].get("name") or name)
    # 规范化世界书
    cb = card["data"].get("character_book")
    if not isinstance(cb, dict):
        card["data"]["character_book"] = {"name": "世界设定", "entries": [], "extensions": {}}
    else:
        card["data"]["character_book"].setdefault("name", "世界设定")
        card["data"]["character_book"].setdefault("entries", [])
        card["data"]["character_book"].setdefault("extensions", {})
    meta = card.get("_meta") if isinstance(card.get("_meta"), dict) else {}
    if draft:
        meta["draftId"] = data.get("id") or cloud_id
        meta["draftVersion"] = data.get("version")
        meta["cloudId"] = None
        meta["source"] = "cloud-draft"
        # 草稿尚未正式发布，db_id 保持 -1
        if not card["data"].get("db_id") or int(card["data"].get("db_id") or -1) <= 0:
            card["data"]["db_id"] = -1
    else:
        meta["cloudId"] = data.get("id") or cloud_id
        meta["source"] = "cloud"
        card["data"]["db_id"] = int(data.get("id") or cloud_id)
    card["_meta"] = meta
    return write_folder(card, local_id=name)


def _trpc_post(path: str, payload: dict) -> dict:
    cookie, token, _, _ = _require_auth(30)
    body = json.dumps({"0": {"json": payload}}, ensure_ascii=False).encode("utf-8")
    st, raw, _ = studio.http(
        f"{studio.get_origin()}/api/trpc/{path}?batch=1",
        cookie,
        token,
        method="POST",
        raw_body=body,
        content_type="application/json",
        timeout=180,
        accept="application/json",
    )
    obj = json.loads(raw.decode("utf-8", "replace"))
    if st != 200:
        err = obj
        if isinstance(obj, list) and obj:
            err = (((obj[0] or {}).get("error") or {}).get("json") or {})
        raise RuntimeError(f"tRPC {path} HTTP {st}: {err}")
    if isinstance(obj, list) and obj:
        data = (((obj[0] or {}).get("result") or {}).get("data") or {}).get("json")
        return data if isinstance(data, dict) else {"value": data}
    if isinstance(obj, dict):
        data = (((obj.get("result") or {}).get("data") or {}).get("json"))
        return data if isinstance(data, dict) else obj
    return {}


def upload_character_image(raw: bytes, filename: str = "image.png") -> str:
    """上传图片到平台，返回 image_url。"""
    if not raw:
        raise ValueError("空图片")
    cookie, token, _, _ = _require_auth(30)
    boundary = "----dzmm" + uuid.uuid4().hex
    fname = Path(filename or "image.png").name or "image.png"
    mime = "image/png"
    low = fname.lower()
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        mime = "image/jpeg"
    elif low.endswith(".webp"):
        mime = "image/webp"
    elif low.endswith(".gif"):
        mime = "image/gif"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{fname}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            raw,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    st, raw, _ = studio.http(
        f"{studio.get_origin()}/api/trpc/studio.uploadCharacterImage?batch=1",
        cookie,
        token,
        method="POST",
        raw_body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
        timeout=120,
        accept="application/json",
    )
    if st != 200:
        raise RuntimeError(f"图片上传失败 HTTP {st}: {raw[:300]!r}")
    obj = json.loads(raw.decode("utf-8", "replace"))
    if isinstance(obj, list) and obj:
        data = (((obj[0] or {}).get("result") or {}).get("data") or {}).get("json") or {}
    else:
        data = (((obj or {}).get("result") or {}).get("data") or {}).get("json") or {}
    url = data.get("image_url") or data.get("url")
    if not url:
        raise RuntimeError(f"图片上传无 URL: {obj}")
    return str(url)


def _is_local_asset_url(url: str) -> bool:
    u = (url or "").strip()
    return bool(u) and not u.startswith(("http://", "https://", "data:")) and (
        u.startswith("assets/") or u.startswith("local://")
    )


def _upload_local_url(local_id: str, url: str, cache: dict[str, str]) -> str:
    rel = url.replace("local://", "").lstrip("/")
    if rel in cache:
        return cache[rel]
    path = resolve_card_asset(local_id, rel)
    cloud = upload_character_image(path.read_bytes(), path.name)
    cache[rel] = cloud
    return cloud


def prepare_raw_data_for_cloud(local_id: str, card: dict) -> dict:
    """把本地 assets 图上传，生成可提交的 rawData（无 _meta）。"""
    if not isinstance(card, dict) or not isinstance(card.get("data"), dict):
        raise ValueError("卡无效")
    data = json.loads(json.dumps(card["data"]))  # deep copy
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("发布前需要角色名")
    cache: dict[str, str] = {}
    images = data.get("image_info") if isinstance(data.get("image_info"), list) else []
    new_images = [dict(it) for it in images if isinstance(it, dict)]
    av0 = str(data.get("avatar_url") or "").strip()
    # 若 avatar_url 指向本地但 image_info 空，补一条
    if not new_images and _is_local_asset_url(av0):
        new_images = [{
            "url": av0.replace("local://", ""),
            "name": "头像",
            "isHidden": False,
            "triggerKeywords": [],
        }]
    # 磁盘有 assets/avatar.* 但字段空时自动挂上
    if not new_images:
        d = _card_dir(local_id)
        assets = d / "assets"
        if assets.is_dir():
            for p in sorted(assets.glob("avatar.*")):
                rel = f"assets/{p.name}"
                new_images = [{
                    "url": rel,
                    "name": "头像",
                    "isHidden": False,
                    "triggerKeywords": [],
                }]
                if not av0:
                    data["avatar_url"] = rel
                break
    # 上传本地图
    fixed_images = []
    for it in new_images:
        item = dict(it)
        u = str(item.get("url") or "")
        if _is_local_asset_url(u):
            item["url"] = _upload_local_url(local_id, u, cache)
            item.pop("local", None)
        fixed_images.append(item)
    data["image_info"] = fixed_images
    av = str(data.get("avatar_url") or "")
    if _is_local_asset_url(av):
        data["avatar_url"] = _upload_local_url(local_id, av, cache)
    elif not av and fixed_images:
        data["avatar_url"] = fixed_images[0].get("url") or ""
    if not data.get("avatar_url") or not data.get("image_info"):
        raise ValueError("发布需要头像/立绘：请先本地导入图片")
    # chat_history 兜底
    chat = data.get("chat_history") if isinstance(data.get("chat_history"), list) else []
    if not chat and str(data.get("first_mes") or "").strip():
        data["chat_history"] = [{
            "id": "1",
            "name": "开场对话",
            "messages": [{"role": "assistant", "content": str(data.get("first_mes"))}],
        }]
    # 清理不宜上传的本地字段
    data.pop("fav", None)
    if data.get("voice_settings") is None:
        data.pop("voice_settings", None)
    if data.get("suggested_replies") in (None, []):
        data.pop("suggested_replies", None)
    cb = data.get("character_book")
    if not isinstance(cb, dict):
        data["character_book"] = {"name": "世界设定", "entries": [], "extensions": {}}
    # extensions：规范化互动模板；空则去掉 status_template
    ext = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}
    ext = dict(ext)
    st = normalize_status_template(ext.get("status_template"))
    if st:
        ext["status_template"] = st
    else:
        ext.pop("status_template", None)
    if ext:
        data["extensions"] = ext
    else:
        data.pop("extensions", None)
    return {
        "spec": card.get("spec") or "chara_card_v3",
        "spec_version": card.get("spec_version") or "3.0",
        "data": data,
    }


def publish_to_cloud(local_id: str, *, as_draft: bool = False) -> dict:
    """上传本地卡到云端。

    - as_draft=True → studio.saveDraft（云端草稿）
    - as_draft=False → studio.uploadOrUpdate（云端保存/更新正式卡）
    """
    local_id = _safe_folder_name(local_id)
    card = load_from_folder(local_id)
    raw_data = prepare_raw_data_for_cloud(local_id, card)
    meta = card.get("_meta") if isinstance(card.get("_meta"), dict) else {}
    db_id = card["data"].get("db_id")
    try:
        db_id = int(db_id) if db_id is not None else -1
    except Exception:
        db_id = -1
    if db_id <= 0 and meta.get("cloudId"):
        try:
            db_id = int(meta["cloudId"])
        except Exception:
            db_id = -1

    if as_draft:
        payload: dict = {"rawData": raw_data}
        draft_id = meta.get("draftId")
        if draft_id:
            payload["id"] = draft_id
        if meta.get("draftVersion") is not None:
            payload["baseVersion"] = meta.get("draftVersion")
        result = _trpc_post("studio.saveDraft", payload)
        # 只更新 meta，保留本地 assets/ 路径，避免下次换图不再上传
        meta["draftId"] = result.get("id") or draft_id
        meta["draftVersion"] = result.get("version")
        meta["source"] = "cloud-draft"
        card = load_from_folder(local_id)
        card["_meta"] = {**(card.get("_meta") or {}), **meta}
        saved = write_folder(card, local_id=local_id)
        return {
            **saved,
            "mode": "draft",
            "cloudId": meta.get("draftId"),
            "result": result,
        }

    payload = {"rawData": raw_data}
    if db_id > 0:
        payload["db_id"] = db_id
    result = _trpc_post("studio.uploadOrUpdate", payload)
    new_id = int(result.get("id") or db_id or 0)
    if new_id <= 0:
        raise RuntimeError(f"云端保存成功但未返回 id: {result}")

    # 官网创作列表：有关联草稿时优先显示草稿；隐藏的正式卡甚至只出现草稿。
    # 因此「保存到云端」在写正式卡后，必须把同内容同步进草稿，否则看起来像没保存。
    raw_data["data"]["db_id"] = new_id
    draft_sync = _sync_character_draft(new_id, raw_data, prefer_draft_id=meta.get("draftId"))

    # 重新读盘：保留本地相对路径；只写入云端 id / meta
    card = load_from_folder(local_id)
    meta = card.get("_meta") if isinstance(card.get("_meta"), dict) else {}
    card["data"]["db_id"] = new_id
    meta["cloudId"] = new_id
    meta["source"] = "cloud"
    if draft_sync.get("draftId"):
        meta["draftId"] = draft_sync["draftId"]
        if draft_sync.get("draftVersion") is not None:
            meta["draftVersion"] = draft_sync["draftVersion"]
    else:
        meta.pop("draftId", None)
        meta.pop("draftVersion", None)
    card["_meta"] = meta
    saved = write_folder(card, local_id=local_id)
    return {
        **saved,
        "mode": "save",
        "cloudId": new_id,
        "characterUrl": f"{studio.get_origin()}/character/{new_id}",
        "result": result,
        "draftSync": draft_sync,
        "clearedDrafts": [],
        "clearedDraftCount": 0,
        "syncedDraftId": draft_sync.get("draftId"),
    }


def _find_draft_ids_for_character(character_id: int) -> list[int]:
    character_id = int(character_id)
    found: list[int] = []
    for it in list_cloud_cards(enrich_listing=False):
        if not it.get("isDraft"):
            continue
        draft_id = int(it.get("cloudId") or 0)
        if draft_id <= 0:
            continue
        linked = it.get("characterId") or it.get("dbId")
        try:
            linked = int(linked) if linked is not None else 0
        except (TypeError, ValueError):
            linked = 0
        if linked == character_id:
            found.append(draft_id)
    return found


def _formal_visible_in_list(character_id: int) -> bool:
    character_id = int(character_id)
    for it in list_cloud_cards(enrich_listing=False):
        if it.get("isDraft"):
            continue
        if int(it.get("cloudId") or 0) == character_id:
            return True
    return False


def _sync_character_draft(
    character_id: int,
    raw_data: dict,
    *,
    prefer_draft_id=None,
) -> dict:
    """把正式卡内容写进关联草稿；没有草稿且正式卡不在列表时新建草稿。"""
    character_id = int(character_id)
    try:
        prefer = int(prefer_draft_id) if prefer_draft_id is not None else 0
    except (TypeError, ValueError):
        prefer = 0
    draft_ids = _find_draft_ids_for_character(character_id)
    targets = []
    if prefer > 0:
        targets.append(prefer)
    for did in draft_ids:
        if did not in targets:
            targets.append(did)

    synced: list[dict] = []
    last: dict = {}
    for draft_id in targets:
        payload: dict = {"rawData": raw_data, "id": draft_id}
        try:
            # 带上 baseVersion 更稳，但缺了也能覆盖；失败再无 version 重试
            result = _trpc_post("studio.saveDraft", payload)
            row = {
                "draftId": int(result.get("id") or draft_id),
                "draftVersion": result.get("version"),
                "ok": True,
                "result": result,
            }
            synced.append(row)
            last = row
        except Exception as e:
            synced.append({"draftId": draft_id, "ok": False, "error": str(e)})

    if synced and any(x.get("ok") for x in synced):
        return {
            "draftId": last.get("draftId"),
            "draftVersion": last.get("draftVersion"),
            "action": "updated",
            "synced": synced,
        }

    # 无可用草稿：若正式卡已在创作列表可见则不必建草稿；隐藏卡则新建以便官网能打开
    if _formal_visible_in_list(character_id):
        return {"draftId": None, "action": "skipped", "synced": synced, "reason": "formal-visible"}

    try:
        result = _trpc_post("studio.saveDraft", {"rawData": raw_data})
        return {
            "draftId": int(result.get("id") or 0) or None,
            "draftVersion": result.get("version"),
            "action": "created",
            "result": result,
            "synced": synced,
        }
    except Exception as e:
        return {"draftId": None, "action": "failed", "error": str(e), "synced": synced}


def _find_local_id_by_cloud_id(cloud_id: int) -> str | None:
    """按 cloudId / db_id 反查本地卡夹名。"""
    cloud_id = int(cloud_id)
    if cloud_id <= 0:
        return None
    root = cards_root()
    if not root.is_dir():
        return None
    for path in root.iterdir():
        if not _is_user_card_dir(path):
            continue
        try:
            card = load_from_folder(path.name)
        except Exception:
            continue
        meta = card.get("_meta") if isinstance(card.get("_meta"), dict) else {}
        try:
            mid = int(meta.get("cloudId") or 0)
        except (TypeError, ValueError):
            mid = 0
        try:
            db_id = int((card.get("data") or {}).get("db_id") or 0)
        except (TypeError, ValueError):
            db_id = 0
        if mid == cloud_id or db_id == cloud_id:
            return path.name
    return None


def shelf_cloud_card(card_id: int, *, listed: bool = True, local_id: str | None = None) -> dict:
    """广场上架：card.publish。过审后 isPublic=true；下架请到官网（控制台不提供 unpublish）。"""
    card_id = int(card_id)
    if card_id <= 0:
        raise ValueError("需要有效 cardId")
    if not listed:
        raise ValueError("控制台不支持下架，请到官网自行操作")
    result = _trpc_post("card.publish", {"cardId": card_id})
    # 上架后常先进入审核；以角色页真实状态为准，不要盲写 isListed=true
    time.sleep(0.4)
    try:
        status = fetch_character_listing_status(card_id)
    except Exception:
        status = {
            "cardId": card_id,
            "isPublic": False,
            "isHidden": False,
            "publishStatus": "pending",
            "firstPublishedAt": "",
            "isPendingReview": True,
            "isListed": False,
        }
    local_name = (local_id or "").strip() or _find_local_id_by_cloud_id(card_id)
    if local_name:
        _sync_local_listing_meta(card_id, status=status, name_hint="")
    saved = None
    if local_name:
        try:
            saved_card = load_from_folder(local_name)
            saved = {
                "localId": local_name,
                "path": str(_card_dir(local_name)),
                "folder": str(_card_dir(local_name)),
                "mtime": folder_mtime(local_name),
                "card": saved_card,
            }
        except Exception:
            saved = None
    out = {
        "mode": "shelf",
        "cardId": card_id,
        "listed": bool(status.get("isListed")),
        "isPublic": status.get("isPublic"),
        "isPendingReview": bool(status.get("isPendingReview")),
        "publishStatus": status.get("publishStatus"),
        "remainingToday": result.get("remainingToday"),
        "dailyLimit": result.get("dailyLimit"),
        "result": result,
        "characterUrl": f"{studio.get_origin()}/character/{card_id}",
        "status": status,
    }
    if saved:
        out.update(
            {
                "localId": saved["localId"],
                "path": saved["path"],
                "folder": saved.get("folder") or saved["path"],
                "mtime": saved.get("mtime") or 0,
                "card": saved["card"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 试玩（平台对话）：createByCard + /api/chat 流式生成
# ---------------------------------------------------------------------------


def _chat_http(
    url: str,
    *,
    method: str = "GET",
    data: dict | None = None,
    raw_body: bytes | None = None,
    accept: str = "application/json",
    timeout: int = 180,
    stream: bool = False,
):
    """角色聊天用 HTTP；Referer 指向 /chat。stream=True 时返回 (status, response, headers)。"""
    cookie, token, _, _ = _require_auth(30)
    headers = {
        "Cookie": cookie,
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 DZMM-Local-Card-Play",
        "Accept": accept,
        "Referer": f"{studio.get_origin()}/chat",
        "Origin": studio.get_origin(),
        "x-dzmm-request-id": f"cardplay{int(time.time()) % 10_000_000}",
    }
    body = None
    if raw_body is not None:
        body = raw_body
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        if stream:
            return resp.status, resp, dict(resp.headers)
        raw = resp.read()
        return resp.status, raw, dict(resp.headers)
    except urllib.error.HTTPError as e:
        if stream:
            # 仍返回可读 body，由调用方决定
            return e.code, e, dict(e.headers)
        return e.code, e.read(), dict(e.headers)


def play_meta(card_id: int) -> dict:
    """聊天用卡摘要 + 开场预览。"""
    card_id = int(card_id)
    if card_id <= 0:
        raise ValueError("需要有效的云端 cardId（已保存正式卡）")
    for_chat = _trpc_get("card.getForChat", {"cardId": card_id})
    preview: dict = {}
    try:
        preview = _trpc_get("card.getQuickChatPreview", {"cardId": card_id}) or {}
    except Exception:
        preview = {}
    return {
        "cardId": card_id,
        "forChat": for_chat if isinstance(for_chat, dict) else {},
        "preview": preview if isinstance(preview, dict) else {},
    }


def play_start(card_id: int, chat_history_index: int | None = None) -> dict:
    """创建平台会话，返回 chatId。"""
    card_id = int(card_id)
    if card_id <= 0:
        raise ValueError("需要有效的云端 cardId")
    # entryPoint 必须是平台枚举：card_detail|home|search|recommendation|
    # home_newbie|chat_list|share_link|quick_chat|profile|checkpoint|
    # telegram|summon|import|general_chat|other
    payload: dict = {"cardId": card_id, "entryPoint": "quick_chat"}
    if chat_history_index is not None:
        try:
            payload["fixedRandomIndex"] = int(chat_history_index)
        except (TypeError, ValueError):
            pass
    result = _trpc_post("chat.createByCard", payload)
    chat_id = result.get("chatId") or result.get("value")
    if not chat_id:
        raise RuntimeError(f"createByCard 未返回 chatId: {result}")
    return {"chatId": str(chat_id), "cardId": card_id}


def play_delete_chat(chat_id: str) -> dict:
    """删除平台会话。官网聊天列表：tRPC chat.deleteChat { chatId }。"""
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise ValueError("缺少 chatId")
    result = _trpc_post("chat.deleteChat", {"chatId": chat_id})
    return {"chatId": chat_id, "result": result}


def play_models() -> dict:
    """角色聊天模型列表（service=chat）。"""
    data = _trpc_get("chat.models", {"service": "chat"})
    return data if isinstance(data, dict) else {"raw": data}


def play_me() -> dict:
    """登录用户资料；{{user}} 显示名取 fullName（与官网一致，非邮箱）。"""
    data = _trpc_get("user.getMe", {})
    if not isinstance(data, dict):
        return {"displayName": "", "raw": data}
    full = str(data.get("fullName") or data.get("name") or "").strip()
    return {
        "displayName": full[:40] if full else "",
        "avatarUrl": str(data.get("avatarUrl") or "").strip(),
        "id": str(data.get("id") or ""),
    }


def play_presets() -> dict:
    """账号预设列表 + 当前激活项 + 登录显示名。"""
    data = _trpc_get("preset.list", {})
    if not isinstance(data, dict):
        data = {}
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    presets = data.get("presets") if isinstance(data.get("presets"), list) else []
    me: dict = {}
    try:
        me = play_me()
    except Exception:
        me = {"displayName": ""}
    return {
        "presets": presets,
        "settings": settings,
        "activePresetIds": settings.get("activePresetIds")
        or ([settings["activePresetId"]] if settings.get("activePresetId") else []),
        "playerInfo": settings.get("playerInfo") or "",
        "displayName": (me.get("displayName") or "").strip(),
    }


def play_messages(chat_id: str) -> dict:
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise ValueError("缺少 chatId")
    data = _trpc_get("chat.getMessages", {"chatId": chat_id})
    return data if isinstance(data, dict) else {"raw": data}


def play_get_settings(chat_id: str) -> dict:
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise ValueError("缺少 chatId")
    data = _trpc_get("chat.getSettings", {"chatId": chat_id})
    return data if isinstance(data, dict) else {}


def play_update_settings(chat_id: str, settings: dict) -> dict:
    """tRPC chat.updateSettings：部分字段 patch（title/style/maxTokens/…）。"""
    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise ValueError("缺少 chatId")
    if not isinstance(settings, dict) or not settings:
        raise ValueError("缺少 settings")
    # 只透传官网侧栏会改的字段，避免误写
    allow = {
        "title",
        "style",
        "maxTokens",
        "model",
        "deepThinking",
        "enableMemoryEnhance",
        "imageGenerationModel",
        "visualNovelMode",
        "backgroundOfficial",
        "backgroundCustom",
        "voiceId",
        "voiceAutoPlay",
        "voiceOnlyQuotes",
        "voiceIgnoreEnglish",
        "voiceIgnoreParentheses",
        "voiceReadAsterisks",
        "presetOverride",
    }
    patch = {k: settings[k] for k in allow if k in settings}
    if "maxTokens" in patch:
        try:
            patch["maxTokens"] = int(patch["maxTokens"])
        except (TypeError, ValueError) as e:
            raise ValueError("maxTokens 无效") from e
    if "title" in patch:
        patch["title"] = str(patch["title"] or "").strip() or "会话"
    if "style" in patch:
        style = str(patch["style"] or "standard").strip()
        if style not in ("standard", "creative", "divergent", "apex_dry"):
            style = "standard"
        patch["style"] = style
    if "imageGenerationModel" in patch:
        img = str(patch["imageGenerationModel"] or "anime").strip()
        if img not in ("anime", "iroha"):
            img = "anime"
        patch["imageGenerationModel"] = img
    if not patch:
        raise ValueError("没有可更新的设置字段")
    data = _trpc_post("chat.updateSettings", {"chatId": chat_id, "settings": patch})
    return data if isinstance(data, dict) else {"ok": True, "raw": data}


def _flatten_play_messages(payload: dict) -> list[dict]:
    """把 getMessages / complete 结构压成 role+content 列表。"""
    out: list[dict] = []
    chunks = payload.get("chunks") if isinstance(payload, dict) else None
    if isinstance(chunks, list):
        for ch in chunks:
            if not isinstance(ch, dict):
                continue
            msgs = ch.get("messages")
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                role = str(m.get("role") or "").strip()
                content = m.get("content")
                if isinstance(content, list):
                    # multimodal → 拼文本
                    parts = []
                    for p in content:
                        if isinstance(p, dict) and p.get("type") == "text":
                            parts.append(str(p.get("text") or ""))
                        elif isinstance(p, str):
                            parts.append(p)
                    content = "".join(parts)
                if role and isinstance(content, str) and content.strip():
                    out.append({"role": role, "content": content})
    return out


def build_generate_body(
    *,
    chat_id: str,
    card_id: int | str,
    content: str,
    model: str | None = None,
    max_tokens: int | None = None,
    deep_thinking: bool = False,
    enable_memory_enhance: bool = False,
    style: str | None = None,
    image_generation_model: str | None = None,
    preset_ids: list[str] | None = None,
    player_info: str | None = None,
    prompts: list[dict] | None = None,
) -> dict:
    # 官网：上下文长度靠 model internalName；maxTokens 为「最大回复 Token」
    think = bool(deep_thinking)
    chat_settings: dict = {
        "deepThinking": think,
        "enableMemoryEnhance": bool(enable_memory_enhance),
        "style": str(style or "standard").strip() or "standard",
    }
    if model:
        chat_settings["model"] = model
    if max_tokens is None:
        max_tokens = 3500 if think else 2500
    try:
        chat_settings["maxTokens"] = int(max_tokens)
    except (TypeError, ValueError):
        chat_settings["maxTokens"] = 3500 if think else 2500
    img = str(image_generation_model or "anime").strip() or "anime"
    if img not in ("anime", "iroha"):
        img = "anime"
    chat_settings["imageGenerationModel"] = img

    preset_config: dict = {
        "presetIds": [str(x) for x in (preset_ids or []) if str(x).strip()],
    }
    if player_info and str(player_info).strip():
        preset_config["playerInfo"] = str(player_info).strip()

    hist: list[dict] = []
    for m in prompts or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        text = m.get("content")
        if role in ("user", "assistant", "ai") and isinstance(text, str):
            hist.append({"role": "assistant" if role == "ai" else role, "content": text})

    return {
        "operation": "generate",
        "chatId": str(chat_id),
        "cardId": card_id,
        "chatSettings": chat_settings,
        "presetConfig": preset_config,
        "prompts": hist,
        "content": str(content or ""),
    }


def play_generate_request(body: dict):
    """发起 POST /api/chat，返回 (status, response_or_error, headers)。调用方负责读流。"""
    if not isinstance(body, dict):
        raise ValueError("generate body 必须是对象")
    url = f"{studio.get_origin()}/api/chat"
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return _chat_http(
        url,
        method="POST",
        raw_body=raw,
        accept="text/event-stream, application/json, */*",
        timeout=300,
        stream=True,
    )


def parse_sse_line(line: str, content_so_far: str = "") -> dict | None:
    """解析一行 `data: {...}` SSE。返回 {type, ...} 或 None。"""
    line = (line or "").strip()
    if not line.startswith("data: "):
        return None
    try:
        obj = json.loads(line[6:])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    typ = obj.get("type")
    data = obj.get("data")
    if typ == "init":
        gen = data.get("generationId") if isinstance(data, dict) else data
        return {"type": "init", "generationId": gen}
    if typ == "token":
        # 平台 token.data 为增量字符串
        chunk = data if isinstance(data, str) else (str(data) if data is not None else "")
        return {"type": "token", "chunk": chunk, "content": content_so_far + chunk}
    if typ == "step":
        step_content = content_so_far
        step_name = None
        if isinstance(data, dict):
            if data.get("content") is not None:
                step_content = str(data.get("content"))
            step_name = data.get("step")
        return {"type": "step", "content": step_content, "step": step_name}
    if typ == "complete":
        return {"type": "complete", "data": data}
    if typ == "error":
        return {"type": "error", "data": data}
    return {"type": typ or "unknown", "data": data}
