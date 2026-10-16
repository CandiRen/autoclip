"""
发布到海外平台（Upload-Post）的 API。

    GET  /publish/upload-post/config                       当前配置（key 打码）
    PUT  /publish/upload-post/config                       保存 key / 默认 profile（保存前先校验 key）
    DELETE /publish/upload-post/config                     删除本地保存的配置
    GET  /publish/upload-post/profiles                     API Key 下的 profile 与已连接平台
    POST /publish/upload-post/{project_id}/clips/{clip_id} 导出 + 提交发布（后台任务，返回 job_id）
    GET  /publish/upload-post/jobs/{job_id}                任务状态；已提交的附带各平台结果（remote）
    GET  /publish/upload-post/requests/{request_id}        直接按 request_id 查 Upload-Post
    GET  /publish/upload-post/{project_id}/records         这个项目提交过的发布记录

桌面 / Docker / 脚本模式都可用（不依赖 check_desktop_mode）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.services import bilibili_publisher as bili
from backend.services import cover as cover_svc
from backend.services import upload_post_publisher as up
from backend.services import repliz_publisher as repliz
from backend.services.caption_generator import caption_generator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/publish", tags=["publish"])


class UploadPostConfigBody(BaseModel):
    api_key: str | None = Field(None, description="Upload-Post API Key；不传则保留已有的")
    user: str | None = Field(None, description="默认 profile（Upload-Post 里的 user）")


class BilibiliConfigBody(BaseModel):
    cookie: str = Field(..., description="浏览器请求头里的 Cookie，需含 SESSDATA、bili_jct、DedeUserID")


class BilibiliPublishBody(BaseModel):
    output_variant_id: str | None = Field(None, pattern=r'^[a-zA-Z0-9_-]{1,100}$', description="已完成的 B站自动成片版本；传入时不重新导出")
    title: str | None = None
    description: str | None = None
    subtitles: bool = True
    title_card: bool = True
    scheduled_date: str | None = None
    timezone: str | None = None
    visibility: str = Field("private", description="private 仅自己 / public 公开")


class PublishBody(BaseModel):
    platforms: list[str] = Field(..., description="tiktok / instagram / youtube / facebook / linkedin / x / threads / pinterest / bluesky …")
    user: str | None = Field(None, description="Upload-Post profile；不填用配置里的默认值")
    preset: str | None = Field(None, description="发布导出预设；不填按平台自动选（竖屏平台 shorts，否则 original）")
    output_variant_id: str | None = Field(None, pattern=r'^[a-zA-Z0-9_-]{1,100}$', description="已完成的自动平台成片版本；传入时不重新导出")
    title: str | None = Field(None, description="标题；不填用切片标题")
    description: str | None = Field(None, description="YouTube / LinkedIn / Facebook / Pinterest 的描述")
    subtitles: bool = True
    title_card: bool = True
    scheduled_date: str | None = Field(None, description="ISO-8601 定时发布")
    timezone: str | None = Field(None, description="IANA 时区，配合 scheduled_date")
    extra: dict[str, Any] = Field(default_factory=dict, description="平台专属字段透传，如 privacy_level、privacyStatus、facebook_page_id、pinterest_board_id")


class CoverConfigBody(BaseModel):
    enabled: bool | None = None
    provider: str | None = Field(None, description="openai / seedream / dashscope")
    model: str | None = None
    api_key: str | None = Field(None, description="不传则保留已有密钥")
    base_url: str | None = None
    ocr_model: str | None = None
    allow_send_frame: bool | None = Field(None, description="是否允许把视频帧发给第三方生图服务，默认关")
    mode: str | None = Field(None, description="text_model 跟随 AI 模型的服务商 / custom 单独配置")


class CoverGenerateBody(BaseModel):
    platform: str = Field("bilibili", description="bilibili 横屏 / douyin 竖屏")
    title: str | None = None
    subtitle: str | None = None
    badge: str | None = None
    content_type: str | None = Field(None, description="knowledge / interview / game / vlog / general")
    force_local: bool = False
    sync: bool = Field(False, description="True 时同步生成（测试用）；默认后台任务")


def _bili_error(exc: Exception) -> HTTPException:
    if isinstance(exc, bili.BilibiliError):
        code = exc.status_code if exc.status_code in (400, 401, 404) else 400
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _http_error(e: Exception) -> HTTPException:
    if isinstance(e, up.UploadPostError):
        code = e.status_code if e.status_code in (401, 402, 403, 404, 409, 422, 429) else 400
        return HTTPException(status_code=code, detail=str(e))
    if isinstance(e, (ValueError, FileNotFoundError)):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@router.get("/upload-post/config")
async def get_upload_post_config():
    cfg = up.load_config()
    return {
        "configured": cfg.configured,
        "source": cfg.source,
        "api_key_masked": cfg.masked_key(),
        "user": cfg.user,
        "base_url": cfg.base_url,
        "platforms": up.PLATFORMS,
    }


@router.put("/upload-post/config")
async def put_upload_post_config(body: UploadPostConfigBody):
    if body.api_key is None and body.user is None:
        raise HTTPException(status_code=400, detail="api_key 或 user 至少传一个")
    if body.api_key is not None:
        key = body.api_key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="api_key 不能为空")
        try:
            verified = up.verify_api_key(up.UploadPostConfig(api_key=key, base_url=up.load_config().base_url))
        except up.UploadPostError as e:
            raise _http_error(e)
    else:
        verified = None
    cfg = up.save_config(api_key=body.api_key, user=body.user)
    if cfg.source == "env":
        # 环境变量优先于文件：文件写了但不会生效，如实告诉调用方
        note = "已保存到文件，但当前进程用的是环境变量 UPLOAD_POST_API_KEY"
    else:
        note = None
    return {"ok": True, "configured": cfg.configured, "source": cfg.source, "api_key_masked": cfg.masked_key(),
            "user": cfg.user, "account": verified, "note": note}


@router.delete("/upload-post/config")
async def delete_upload_post_config():
    up.clear_config()
    return {"ok": True, "configured": up.load_config().configured}


@router.get("/upload-post/profiles")
async def get_upload_post_profiles():
    try:
        return {"profiles": up.list_profiles()}
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)


@router.post("/upload-post/{project_id}/clips/{clip_id}")
async def start_upload_post_publish(project_id: str, clip_id: str, body: PublishBody):
    from backend.services.publish_export import PRESETS, load_clip_meta
    try:
        load_clip_meta(project_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        platforms = up.normalize_platforms(body.platforms)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if body.preset and body.preset not in PRESETS:
        raise HTTPException(status_code=400, detail=f"未知预设: {body.preset}（可选 {', '.join(PRESETS)}）")
    cfg = up.load_config()
    if not cfg.configured:
        raise HTTPException(status_code=400, detail="没有配置 Upload-Post API Key（PUT /publish/upload-post/config 或环境变量 UPLOAD_POST_API_KEY）")
    if not (body.user or cfg.user):
        raise HTTPException(status_code=400, detail="没有指定 Upload-Post profile（user）")
    return up.start_publish(up.PublishRequest(
        project_id=project_id, clip_id=clip_id, platforms=platforms, user=body.user, preset=body.preset, output_variant_id=body.output_variant_id,
        title=body.title, description=body.description, subtitles=body.subtitles, title_card=body.title_card,
        scheduled_date=body.scheduled_date, timezone=body.timezone, extra=body.extra,
    ))


@router.get("/upload-post/jobs/{job_id}")
async def get_upload_post_job(job_id: str):
    job = up.get_publish_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="没有这个发布任务")
    return job


@router.get("/upload-post/requests/{request_id}")
async def get_upload_post_request(request_id: str, project_id: str | None = None):
    try:
        return up.get_status(request_id, project_id=project_id)
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)


@router.get("/upload-post/{project_id}/records")
async def list_upload_post_records(project_id: str):
    return {"records": up.list_records(project_id)}


@router.delete("/upload-post/{project_id}/records/{request_id}")
async def cancel_upload_post_record(project_id: str, request_id: str):
    try:
        return up.cancel_record(project_id, request_id)
    except bili.BilibiliError as e:
        raise _bili_error(e)
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)


@router.get("/bilibili/config")
async def get_bilibili_config():
    return bili.load_config().public()


@router.put("/bilibili/config")
async def put_bilibili_config(body: BilibiliConfigBody):
    cookie = body.cookie.strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie 不能为空")
    try:
        account = bili.verify_cookie(cookie)
    except bili.BilibiliError as e:
        raise _bili_error(e)
    cfg = bili.save_config(account["cookie"], account["nickname"], account["uid"])
    payload = {"ok": True, **cfg.public()}
    if cfg.source == "env":
        payload["note"] = "已保存到文件，但当前进程用的是环境变量 BILIBILI_COOKIE"
    return payload


@router.delete("/bilibili/config")
async def delete_bilibili_config():
    bili.clear_config()
    return {"ok": True, **bili.load_config().public()}


@router.post("/bilibili/{project_id}/clips/{clip_id}")
async def start_bilibili_publish(project_id: str, clip_id: str, body: BilibiliPublishBody):
    from backend.services.publish_export import load_clip_meta
    try:
        load_clip_meta(project_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if body.visibility not in ("private", "public"):
        raise HTTPException(status_code=400, detail="可见范围只能是 private 或 public")
    if not bili.load_config().configured:
        raise HTTPException(status_code=400, detail="还没有配置 B 站账号")
    return bili.start_publish(bili.BilibiliPublishRequest(
        project_id=project_id,
        clip_id=clip_id,
        output_variant_id=body.output_variant_id,
        title=body.title,
        description=body.description,
        subtitles=body.subtitles,
        title_card=body.title_card,
        scheduled_date=body.scheduled_date,
        timezone=body.timezone,
        visibility=body.visibility,
    ))


@router.get("/bilibili/jobs/{job_id}")
async def get_bilibili_job(job_id: str):
    job = bili.get_publish_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="没有这个发布任务")
    return job


def _cover_error(exc: Exception) -> HTTPException:
    if isinstance(exc, cover_svc.CoverError):
        code = exc.status_code if exc.status_code in (400, 401, 404) else 400
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/covers/config")
async def get_cover_config():
    return cover_svc.load_config().public()


@router.put("/covers/config")
async def put_cover_config(body: CoverConfigBody):
    if body.mode is not None and body.mode not in ("text_model", "custom"):
        raise HTTPException(status_code=400, detail="mode 只能是 text_model 或 custom")
    if body.mode != "text_model" and body.provider is not None and body.provider.strip().lower() not in ("openai", "seedream", "dashscope"):
        raise HTTPException(status_code=400, detail="生图提供商只能是 openai、seedream 或 dashscope")
    cfg = cover_svc.save_config(
        enabled=body.enabled,
        provider=body.provider,
        model=body.model,
        api_key=body.api_key,
        base_url=body.base_url,
        ocr_model=body.ocr_model,
        allow_send_frame=body.allow_send_frame,
        mode=body.mode,
    )
    payload = {"ok": True, **cfg.public()}
    if cfg.source == "env":
        payload["note"] = "密钥来自环境变量 IMAGE_API_KEY，这里的修改不会覆盖它。"
    return payload


@router.delete("/covers/config")
async def delete_cover_config():
    return {"ok": True, **cover_svc.clear_config().public()}


@router.get("/covers/{project_id}/clips/{clip_id}")
async def get_clip_cover(project_id: str, clip_id: str, platform: str = "bilibili"):
    from backend.services.publish_export import load_clip_meta
    try:
        load_clip_meta(project_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    found = cover_svc.get_cover(project_id, clip_id, platform)
    if not found:
        return {"ok": False, "platform": cover_svc.normalize_platform(platform), "url": None}
    return found


@router.get("/covers/{project_id}/clips/{clip_id}/file")
async def get_clip_cover_file(project_id: str, clip_id: str, platform: str = "bilibili"):
    platform = cover_svc.normalize_platform(platform)
    path = cover_svc.cover_path(project_id, clip_id, platform)
    if not path.exists() or path.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="还没有这张封面")
    return FileResponse(path, media_type="image/jpeg", filename=f"{clip_id}_{platform}.jpg")


@router.post("/covers/{project_id}/clips/{clip_id}")
async def start_cover_generate(project_id: str, clip_id: str, body: CoverGenerateBody):
    from backend.services.publish_export import load_clip_meta
    try:
        load_clip_meta(project_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    platform = cover_svc.normalize_platform(body.platform)
    if body.sync:
        try:
            return cover_svc.generate_cover(
                project_id=project_id,
                clip_id=clip_id,
                platform=platform,
                title=body.title,
                subtitle=body.subtitle,
                badge=body.badge,
                content_type=body.content_type,
                force_local=body.force_local,
            )
        except Exception as e:  # noqa: BLE001
            raise _cover_error(e)
    return cover_svc.start_generate(
        project_id=project_id,
        clip_id=clip_id,
        platform=platform,
        title=body.title,
        subtitle=body.subtitle,
        badge=body.badge,
        content_type=body.content_type,
        force_local=body.force_local,
    )


@router.get("/covers/jobs/{job_id}")
async def get_cover_job(job_id: str):
    job = cover_svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="没有这个封面任务")
    return job


@router.post("/covers/jobs/{job_id}/cancel")
async def cancel_cover_job(job_id: str):
    return cover_svc.cancel_job(job_id)


# ── AI Caption & Hashtag Generation ──────────────────────────────────────────

class GenerateCaptionBody(BaseModel):
    project_id: str | None = None
    clip_id: str | None = None
    title: str | None = None
    transcript: str | None = None
    platform: str = "threads"
    language: str = "id"
    style: str = "viral"
    custom_prompt: str | None = None


@router.post("/generate-caption")
async def generate_caption(body: GenerateCaptionBody):
    """
    Generate high-quality, relevant social media captions and hashtags
    tailored to specific platforms and styles.
    """
    text_content = body.transcript or ""
    clip_title = body.title or ""
    clip_outline = ""

    # If project_id & clip_id are provided, attempt to enrich context from clip metadata
    if body.project_id and body.clip_id:
        try:
            from backend.services.publish_export import load_clip_meta
            meta = load_clip_meta(body.project_id, body.clip_id)
            if not clip_title:
                clip_title = meta.get("generated_title") or meta.get("title") or ""
            clip_outline = meta.get("outline") or ""
            if not text_content:
                text_content = meta.get("content") or meta.get("transcript") or ""
        except Exception:
            pass

    try:
        res = caption_generator.generate(
            title=clip_title,
            transcript=text_content,
            platform=body.platform,
            language=body.language,
            custom_prompt=body.custom_prompt or "",
            clip_outline=clip_outline,
        )
        return {
            "ok": True,
            "title": res.title,
            "topic": res.topic,
            "hashtags": res.hashtags,
            "best_caption": res.best_caption,
            "variations": [
                {
                    "style": v.style,
                    "style_label": v.style_label,
                    "caption": v.caption,
                    "hashtags": v.hashtags,
                }
                for v in res.variations
            ],
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to generate caption: %s", e)
        raise HTTPException(status_code=500, detail=f"Gagal membuat caption AI: {e}")


# ── Repliz.com Social Publishing ─────────────────────────────────────────────

def _repliz_error(exc: Exception) -> HTTPException:
    if isinstance(exc, repliz.ReplizError):
        code = exc.status_code if exc.status_code in (400, 401, 402, 403, 404, 422, 429, 502) else 400
        return HTTPException(status_code=code, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


class ReplizConfigBody(BaseModel):
    access_key: str | None = Field(None, description="Repliz Access Key")
    secret_key: str | None = Field(None, description="Repliz Secret Key")
    default_account_id: str | None = Field(None, description="Default Repliz connected Account ID")


class ReplizPublishBody(BaseModel):
    account_id: str = Field(..., description="Repliz target account ID")
    post_type: str = Field("video", description="text, video, image, reel, album, link")
    title: str | None = Field(None, description="Post title (e.g. for YouTube/media)")
    description: str | None = Field(None, description="Main caption text")
    topic: str | None = Field(None, description="Optional topic category for Threads")
    scheduled_date: str | None = Field(None, description="ISO-8601 UTC timestamp")
    media_url: str | None = Field(None, description="Public URL of attached media")
    reply_text: str | None = Field(None, description="Optional follow-up reply in thread")
    replies: list[dict[str, Any]] = Field(default_factory=list, description="Follow-up chain replies")
    subtitles: bool = True
    title_card: bool = True


class ReplizUpdateScheduleBody(BaseModel):
    description: str | None = None
    schedule_at: str | None = None
    topic: str | None = None
    title: str | None = None


@router.get("/repliz/config")
async def get_repliz_config():
    cfg = repliz.load_config()
    return cfg.public()


@router.put("/repliz/config")
async def put_repliz_config(body: ReplizConfigBody):
    if body.access_key is None and body.secret_key is None and body.default_account_id is None:
        raise HTTPException(status_code=400, detail="access_key, secret_key, atau default_account_id harus diisi")

    # If updating keys, test connection first
    cfg = repliz.load_config()
    test_acc = body.access_key if body.access_key is not None else cfg.access_key
    test_sec = body.secret_key if body.secret_key is not None else cfg.secret_key

    if test_acc and test_sec:
        try:
            verified = repliz.verify_credentials(test_acc, test_sec)
        except Exception as e:  # noqa: BLE001
            raise _repliz_error(e)
    else:
        verified = None

    saved_cfg = repliz.save_config(
        access_key=body.access_key,
        secret_key=body.secret_key,
        default_account_id=body.default_account_id,
    )

    note = "Konfigurasi berhasil disimpan."
    if saved_cfg.source == "env":
        note = "Tersimpan ke file, namun proses saat ini memprioritaskan variabel lingkungan REPLIZ_ACCESS_KEY."
    elif saved_cfg.source == "post_env":
        note = "Kredensial aktif terbaca otomatis dari /home/cr/project/post/.env."

    return {
        "ok": True,
        **saved_cfg.public(),
        "account_info": verified,
        "note": note,
    }


@router.delete("/repliz/config")
async def delete_repliz_config():
    cfg = repliz.clear_config()
    return {"ok": True, **cfg.public()}


@router.get("/repliz/accounts")
async def get_repliz_accounts():
    try:
        client = repliz.ReplizClient()
        accounts = client.list_accounts()
        return {"accounts": accounts}
    except Exception as e:  # noqa: BLE001
        raise _repliz_error(e)


@router.get("/repliz/accounts/{account_id}/stats")
async def get_repliz_account_stats(account_id: str):
    try:
        client = repliz.ReplizClient()
        return client.get_account_statistics(account_id)
    except Exception as e:  # noqa: BLE001
        raise _repliz_error(e)


@router.get("/repliz/schedules")
async def get_repliz_schedules(account_id: str | None = None, page: int = 1, limit: int = 50):
    try:
        client = repliz.ReplizClient()
        return client.list_schedules(account_id=account_id, page=page, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise _repliz_error(e)


@router.post("/repliz/{project_id}/clips/{clip_id}")
async def start_repliz_publish(project_id: str, clip_id: str, body: ReplizPublishBody):
    cfg = repliz.load_config()
    if not cfg.configured:
        raise HTTPException(
            status_code=400,
            detail="Koneksi Repliz belum dikonfigurasi. Atur access key & secret key di Pengaturan.",
        )
    return repliz.start_publish(
        repliz.ReplizPublishRequest(
            project_id=project_id,
            clip_id=clip_id,
            account_id=body.account_id,
            post_type=body.post_type,
            title=body.title,
            description=body.description,
            topic=body.topic,
            scheduled_date=body.scheduled_date,
            media_url=body.media_url,
            reply_text=body.reply_text,
            replies=body.replies,
            subtitles=body.subtitles,
            title_card=body.title_card,
        )
    )


@router.get("/repliz/jobs/{job_id}")
async def get_repliz_job(job_id: str):
    job = repliz.get_publish_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Tugas publikasi Repliz tidak ditemukan.")
    return job


@router.get("/repliz/{project_id}/records")
async def list_repliz_records(project_id: str):
    return {"records": repliz.list_records(project_id)}


@router.delete("/repliz/{project_id}/records/{schedule_id}")
async def cancel_repliz_record(project_id: str, schedule_id: str):
    try:
        return repliz.cancel_record(project_id, schedule_id)
    except Exception as e:  # noqa: BLE001
        raise _repliz_error(e)


@router.post("/repliz/schedules/{schedule_id}/retry")
async def retry_repliz_schedule(schedule_id: str):
    try:
        client = repliz.ReplizClient()
        return client.retry_schedule(schedule_id)
    except Exception as e:  # noqa: BLE001
        raise _repliz_error(e)


@router.delete("/repliz/schedules/{schedule_id}")
async def delete_repliz_schedule(schedule_id: str):
    try:
        client = repliz.ReplizClient()
        return client.delete_schedule(schedule_id)
    except Exception as e:  # noqa: BLE001
        raise _repliz_error(e)


@router.get("/repliz/threads/search")
async def search_repliz_threads(account_id: str, query: str, limit: int = 20):
    try:
        client = repliz.ReplizClient()
        return client.search_threads_content(account_id=account_id, query=query, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise _repliz_error(e)


@router.get("/repliz/trends/tiktok")
async def get_repliz_tiktok_trends(account_id: str, genre: str | None = None, country: str | None = None):
    try:
        client = repliz.ReplizClient()
        return client.get_tiktok_trending_music(account_id=account_id, genre=genre, country=country)
    except Exception as e:  # noqa: BLE001
        raise _repliz_error(e)

