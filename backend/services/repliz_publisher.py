"""
Repliz.com Integration Service for AutoClip.
Connects with Repliz Streamable HTTP MCP (Model Context Protocol) API to manage accounts,
schedule and publish posts to Threads, Instagram, TikTok, Facebook, Shopee, Telegram, etc.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

MCP_ENDPOINT = "https://mcp.repliz.com/mcp"
ENV_POST_PATH = Path("/home/cr/project/post/.env")

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


class ReplizError(Exception):
    def __init__(self, message: str, status_code: int = 400, data: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data


@dataclass
class ReplizConfig:
    access_key: str = ""
    secret_key: str = ""
    default_account_id: str | None = None
    configured: bool = False
    source: str = "none"  # "file", "env", "post_env", "none"

    def masked_access_key(self) -> str:
        if not self.access_key:
            return ""
        if len(self.access_key) <= 8:
            return self.access_key[:2] + "****"
        return self.access_key[:4] + "****" + self.access_key[-4:]

    def masked_secret_key(self) -> str:
        if not self.secret_key:
            return ""
        return self.secret_key[:4] + "****" + self.secret_key[-4:]

    def public(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "source": self.source,
            "access_key_masked": self.masked_access_key(),
            "secret_key_masked": self.masked_secret_key(),
            "default_account_id": self.default_account_id,
        }


def _config_file_path() -> Path:
    data_dir = os.environ.get("AUTOCLIP_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "repliz.json"
    return Path("data/repliz.json")


def _read_post_env() -> dict[str, str]:
    vals: dict[str, str] = {}
    if ENV_POST_PATH.exists():
        try:
            for line in ENV_POST_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"\'')
        except Exception:
            pass
    return vals


def load_config() -> ReplizConfig:
    """Load Repliz configuration with fallback hierarchy: file -> environment -> /home/cr/project/post/.env"""
    # 1. Environment variables
    env_acc = os.environ.get("REPLIZ_ACCESS_KEY", "").strip()
    env_sec = os.environ.get("REPLIZ_SECRET_KEY", "").strip()
    env_acc_id = os.environ.get("REPLIZ_ACCOUNT_ID", "").strip()
    if env_acc and env_sec:
        return ReplizConfig(
            access_key=env_acc,
            secret_key=env_sec,
            default_account_id=env_acc_id or None,
            configured=True,
            source="env",
        )

    # 2. Local config file
    cfg_path = _config_file_path()
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            acc = data.get("access_key", "").strip()
            sec = data.get("secret_key", "").strip()
            if acc and sec:
                return ReplizConfig(
                    access_key=acc,
                    secret_key=sec,
                    default_account_id=data.get("default_account_id") or None,
                    configured=True,
                    source="file",
                )
        except Exception as e:
            logger.warning("Failed to read repliz config file %s: %s", cfg_path, e)

    # 3. Fallback to /home/cr/project/post/.env
    post_env = _read_post_env()
    p_acc = post_env.get("REPLIZ_ACCESS_KEY", "").strip()
    p_sec = post_env.get("REPLIZ_SECRET_KEY", "").strip()
    p_acc_id = post_env.get("REPLIZ_ACCOUNT_ID", "").strip()
    if p_acc and p_sec:
        return ReplizConfig(
            access_key=p_acc,
            secret_key=p_sec,
            default_account_id=p_acc_id or None,
            configured=True,
            source="post_env",
        )

    return ReplizConfig()


def save_config(
    access_key: str | None = None,
    secret_key: str | None = None,
    default_account_id: str | None = None,
) -> ReplizConfig:
    cfg = load_config()
    new_acc = access_key.strip() if access_key is not None else cfg.access_key
    new_sec = secret_key.strip() if secret_key is not None else cfg.secret_key
    new_acc_id = default_account_id.strip() if default_account_id is not None else cfg.default_account_id

    cfg_path = _config_file_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_key": new_acc,
        "secret_key": new_sec,
        "default_account_id": new_acc_id,
    }
    cfg_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return load_config()


def clear_config() -> ReplizConfig:
    cfg_path = _config_file_path()
    if cfg_path.exists():
        try:
            cfg_path.unlink()
        except Exception:
            pass
    return load_config()


class ReplizClient:
    """Client for Repliz Streamable HTTP MCP server."""

    def __init__(self, access_key: str | None = None, secret_key: str | None = None):
        cfg = load_config()
        self.access_key = access_key or cfg.access_key
        self.secret_key = secret_key or cfg.secret_key
        if not self.access_key or not self.secret_key:
            raise ReplizError("Repliz Access Key atau Secret Key belum dikonfigurasi.", status_code=401)

        basic_token = base64.b64encode(f"{self.access_key}:{self.secret_key}".encode()).decode()
        self.base_headers = {
            "Authorization": f"Basic {basic_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id: str | None = None
        self._req_id = 0

    def _rpc_call(self, method: str, params: dict, timeout: int = 30) -> tuple[int, dict, Any]:
        self._req_id += 1
        headers = {**self.base_headers}
        if self.session_id and method != "initialize":
            headers["Mcp-Session-Id"] = self.session_id

        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._req_id}
        try:
            resp = requests.post(MCP_ENDPOINT, headers=headers, json=payload, timeout=timeout)
        except Exception as e:
            raise ReplizError(f"Gagal menghubungi endpoint Repliz: {e}", status_code=502)

        ct = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ct:
            lines = [l[5:].strip() for l in resp.text.splitlines() if l.startswith("data:")]
            parsed = [json.loads(l) for l in lines if l]
            return resp.status_code, dict(resp.headers), parsed

        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return resp.status_code, dict(resp.headers), body

    def init_session(self) -> str | None:
        status, headers, result = self._rpc_call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "autoclip-repliz", "version": "1.0"},
            },
        )
        sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if not sid and isinstance(result, dict) and "result" in result:
            sid = result["result"].get("session_id") or result["result"].get("sessionId")
        if not sid and isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and "result" in item:
                    sid = item["result"].get("session_id") or item["result"].get("sessionId")
        self.session_id = sid
        return self.session_id

    def tool_call(self, tool_name: str, arguments: dict, timeout: int = 35) -> Any:
        if not self.session_id:
            self.init_session()

        status, _, res = self._rpc_call("tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout)
        # Check if session expired
        is_session_err = False
        if status in (400, 401, 404):
            is_session_err = True
        elif isinstance(res, dict) and "error" in res:
            err_msg = str(res.get("error", {})).lower()
            if "session" in err_msg or "unauthorized" in err_msg:
                is_session_err = True

        if is_session_err:
            self.init_session()
            status, _, res = self._rpc_call("tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout)

        is_error, content = self._extract_result(res)
        if is_error:
            msg = content if isinstance(content, str) else json.dumps(content)
            raise ReplizError(f"Repliz Tool Error ({tool_name}): {msg}", status_code=status or 400, data=content)
        return content

    @staticmethod
    def _extract_result(res: Any) -> tuple[bool, Any]:
        items = res if isinstance(res, list) else [res]
        for item in items:
            if isinstance(item, dict) and "result" in item:
                content = item["result"].get("content", [])
                is_error = item["result"].get("isError", False)
                if content and isinstance(content, list):
                    text = content[0].get("text", "")
                    try:
                        return is_error, json.loads(text)
                    except Exception:
                        return is_error, text
                return is_error, item["result"]
            if isinstance(item, dict) and "error" in item:
                return True, item["error"]
        return False, res

    # ── High level features ────────────────────────────────────────────────────

    def list_accounts(self) -> list[dict[str, Any]]:
        """List all connected accounts (Threads, IG, TikTok, FB, etc.)."""
        data = self.tool_call("repliz_list_accounts", {})
        if isinstance(data, dict):
            return data.get("docs", [])
        if isinstance(data, list):
            return data
        return []

    def get_account_statistics(self, account_id: str) -> dict[str, Any]:
        """Get analytics and statistics for a connected account."""
        return self.tool_call("repliz_get_account_statistics", {"accountId": account_id})

    def list_schedules(self, account_id: str | None = None, page: int = 1, limit: int = 50) -> dict[str, Any]:
        """List scheduled posts, optionally filtered by accountId."""
        args: dict[str, Any] = {"page": page, "limit": limit}
        if account_id:
            args["accountId"] = account_id
        return self.tool_call("repliz_list_schedules", args)

    def create_schedule(
        self,
        *,
        account_id: str,
        post_type: str,
        description: str,
        schedule_at: str,
        title: str | None = None,
        topic: str | None = None,
        medias: list[dict[str, Any]] | None = None,
        replies: list[dict[str, Any]] | None = None,
        additional_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Schedule a post to be published to a connected account.
        post_type: 'text', 'image', 'video', 'reel', 'album', 'story', 'link'
        schedule_at: ISO 8601 UTC timestamp, e.g. '2026-10-17T09:00:00Z'
        """
        args: dict[str, Any] = {
            "accountId": account_id,
            "type": post_type,
            "description": description or "",
            "scheduleAt": schedule_at,
        }
        if title:
            args["title"] = title
        if topic:
            args["topic"] = topic
        if medias:
            args["medias"] = medias
        if replies:
            args["replies"] = replies
        if additional_info:
            args["additionalInfo"] = additional_info

        return self.tool_call("repliz_create_schedule", args)

    def update_schedule(self, schedule_id: str, **kwargs) -> dict[str, Any]:
        args = {"scheduleId": schedule_id, **kwargs}
        return self.tool_call("repliz_update_schedule", args)

    def retry_schedule(self, schedule_id: str) -> dict[str, Any]:
        return self.tool_call("repliz_retry_schedule", {"scheduleId": schedule_id})

    def delete_schedule(self, schedule_id: str) -> dict[str, Any]:
        return self.tool_call("repliz_delete_schedule", {"scheduleId": schedule_id})

    def search_threads_content(self, account_id: str, query: str, limit: int = 20) -> dict[str, Any]:
        return self.tool_call("repliz_search_threads_content", {"accountId": account_id, "query": query, "limit": limit})

    def search_threads_user(self, account_id: str, username: str) -> dict[str, Any]:
        return self.tool_call("repliz_search_threads_user", {"accountId": account_id, "username": username})

    def get_tiktok_trending_music(self, account_id: str, genre: str | None = None, country: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"accountId": account_id}
        if genre:
            args["genre"] = genre
        if country:
            args["country"] = country
        return self.tool_call("repliz_tiktok_trending_music", args)


def verify_credentials(access_key: str, secret_key: str) -> dict[str, Any]:
    """Test connection with Repliz MCP and return summary of connected accounts."""
    client = ReplizClient(access_key=access_key, secret_key=secret_key)
    session_id = client.init_session()
    accounts = client.list_accounts()
    return {
        "ok": True,
        "session_id": session_id,
        "accounts_count": len(accounts),
        "accounts": [
            {
                "id": a.get("_id") or a.get("id"),
                "name": a.get("name"),
                "username": a.get("username"),
                "type": a.get("type"),
                "picture": a.get("picture"),
                "isConnected": a.get("isConnected", True),
            }
            for a in accounts
        ],
    }


# ── Publishing Jobs & Project Records ──────────────────────────────────────────

@dataclass
class ReplizPublishRequest:
    project_id: str
    clip_id: str
    account_id: str
    post_type: str = "video"  # 'text', 'video', 'image', 'reel', 'album'
    title: str | None = None
    description: str | None = None
    topic: str | None = None
    scheduled_date: str | None = None  # ISO 8601
    media_url: str | None = None
    replies: list[dict[str, Any]] = field(default_factory=list)
    reply_text: str | None = None
    subtitles: bool = True
    title_card: bool = True


def _records_file(project_id: str) -> Path:
    data_dir = os.environ.get("AUTOCLIP_DATA_DIR")
    base = Path(data_dir) if data_dir else Path("data")
    return base / "projects" / project_id / "repliz_records.json"


def list_records(project_id: str) -> list[dict[str, Any]]:
    path = _records_file(project_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_record(project_id: str, record: dict[str, Any]) -> None:
    path = _records_file(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = list_records(project_id)
    # Update existing or prepend new
    rid = record.get("schedule_id")
    filtered = [r for r in records if r.get("schedule_id") != rid]
    filtered.insert(0, record)
    path.write_text(json.dumps(filtered, indent=2, ensure_ascii=False), encoding="utf-8")


def start_publish(req: ReplizPublishRequest) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "status": "queued",
        "percent": 0,
        "schedule_id": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        _jobs[job_id] = job

    t = threading.Thread(target=_publish_worker, args=(job_id, req), daemon=True)
    t.start()
    return {"ok": True, "job_id": job_id, "status": "queued"}


def get_publish_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        return _jobs.get(job_id)


def _publish_worker(job_id: str, req: ReplizPublishRequest) -> None:
    try:
        with _lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["percent"] = 20

        client = ReplizClient()
        # 1. Determine scheduled time (UTC ISO 8601)
        if req.scheduled_date:
            try:
                # Normalize to ISO 8601 UTC
                dt = datetime.fromisoformat(req.scheduled_date.replace("Z", "+00:00"))
                schedule_at = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                schedule_at = req.scheduled_date
        else:
            # Immediate: Repliz requires a future scheduleAt timestamp, set to now + 2 mins
            schedule_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        medias = []
        if req.post_type in ("video", "reel") and req.media_url:
            medias.append({"type": "video", "url": req.media_url})
        elif req.post_type == "image" and req.media_url:
            medias.append({"type": "image", "url": req.media_url})

        # Replies
        replies = list(req.replies)
        if req.reply_text and req.reply_text.strip():
            replies.append({"type": "text", "description": req.reply_text.strip()})

        with _lock:
            _jobs[job_id]["percent"] = 60

        # Call Repliz
        res = client.create_schedule(
            account_id=req.account_id,
            post_type=req.post_type,
            description=req.description or "",
            schedule_at=schedule_at,
            title=req.title,
            topic=req.topic,
            medias=medias if medias else None,
            replies=replies if replies else None,
        )

        schedule_id = (
            res.get("scheduleId") or res.get("id") or res.get("_id") or (res if isinstance(res, str) else str(res))
        )

        record = {
            "schedule_id": str(schedule_id),
            "job_id": job_id,
            "project_id": req.project_id,
            "clip_id": req.clip_id,
            "account_id": req.account_id,
            "type": req.post_type,
            "title": req.title,
            "description": req.description,
            "topic": req.topic,
            "schedule_at": schedule_at,
            "status": "scheduled",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_record(req.project_id, record)

        with _lock:
            _jobs[job_id]["status"] = "submitted"
            _jobs[job_id]["percent"] = 100
            _jobs[job_id]["schedule_id"] = str(schedule_id)
            _jobs[job_id]["result"] = res

    except Exception as e:
        logger.exception("Repliz publish failed: %s", e)
        with _lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(e)


def cancel_record(project_id: str, schedule_id: str) -> dict[str, Any]:
    client = ReplizClient()
    try:
        client.delete_schedule(schedule_id)
    except Exception as e:
        logger.warning("Repliz delete schedule returned: %s", e)

    # Mark local record cancelled
    path = _records_file(project_id)
    records = list_records(project_id)
    for r in records:
        if r.get("schedule_id") == schedule_id:
            r["status"] = "cancelled"
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "status": "cancelled", "schedule_id": schedule_id}
