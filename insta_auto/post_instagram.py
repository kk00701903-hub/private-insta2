from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from uuid import uuid4

import httpx

from insta_auto.config import AppConfig

GRAPH_VERSION = "v22.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"


class InstagramError(RuntimeError):
    pass


class ImageHostError(RuntimeError):
    pass


def _caption(caption: str, hashtags: list[str]) -> str:
    tag_line = " ".join(f"#{t.lstrip('#')}" for t in hashtags if t)
    parts = [p for p in ((caption or "").strip(), tag_line) if p]
    return "\n\n".join(parts)[:2200]


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> dict:
    response = await client.request(method, url, **kwargs)
    payload = response.json()
    if not response.is_success or "error" in payload:
        err = payload.get("error") or payload
        raise InstagramError(f"Graph API error: {err}")
    return payload


async def _wait_finished(
    client: httpx.AsyncClient,
    creation_id: str,
    token: str,
    *,
    attempts: int = 90,
) -> None:
    url = f"{GRAPH}/{creation_id}"
    for _ in range(attempts):
        payload = await _request(
            client,
            "GET",
            url,
            params={"fields": "status_code,status", "access_token": token},
        )
        code = payload.get("status_code")
        if code == "FINISHED":
            return
        if code in {"ERROR", "EXPIRED"}:
            raise InstagramError(f"Container {creation_id} failed: {payload}")
        await asyncio.sleep(2)
    raise InstagramError(f"Timed out waiting for container {creation_id}")


def _public_url(cfg: AppConfig, path: Path) -> str:
    dest_dir = cfg.public_media_dir or Path("data/public")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{path.stem}_{uuid4().hex[:8]}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    assert cfg.public_base_url
    return f"{cfg.public_base_url.rstrip('/')}/{dest.name}"


async def _upload_imgbb(cfg: AppConfig, path: Path) -> str:
    assert cfg.imgbb_api_key
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.imgbb.com/1/upload",
            data={"key": cfg.imgbb_api_key},
            files={"image": (path.name, path.read_bytes(), "image/png")},
        )
    payload = response.json()
    if not response.is_success or not payload.get("success"):
        raise ImageHostError(f"ImgBB upload failed: {payload}")
    return str(payload["data"]["url"])


async def _upload_cloudinary(cfg: AppConfig, path: Path) -> str:
    assert cfg.cloudinary_cloud_name and cfg.cloudinary_api_key and cfg.cloudinary_api_secret
    timestamp = int(time.time())
    to_sign = f"timestamp={timestamp}{cfg.cloudinary_api_secret}"
    signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
    url = f"https://api.cloudinary.com/v1_1/{cfg.cloudinary_cloud_name}/image/upload"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            data={
                "api_key": cfg.cloudinary_api_key,
                "timestamp": str(timestamp),
                "signature": signature,
            },
            files={"file": (path.name, path.read_bytes(), "image/png")},
        )
    payload = response.json()
    if not response.is_success or "secure_url" not in payload:
        raise ImageHostError(f"Cloudinary upload failed: {payload}")
    return str(payload["secure_url"])


async def upload_image(cfg: AppConfig, path: Path) -> str:
    path = Path(path)
    if cfg.public_base_url:
        return _public_url(cfg, path)
    if cfg.cloudinary_cloud_name and cfg.cloudinary_api_key and cfg.cloudinary_api_secret:
        return await _upload_cloudinary(cfg, path)
    if cfg.imgbb_api_key:
        return await _upload_imgbb(cfg, path)
    raise ImageHostError(
        "No image host configured. Set IMGBB_API_KEY, Cloudinary keys, or PUBLIC_BASE_URL."
    )


async def upload_images(cfg: AppConfig, paths: list[Path]) -> list[str]:
    return [await upload_image(cfg, p) for p in paths]


async def publish_carousel(
    cfg: AppConfig,
    image_paths: list[Path],
    *,
    caption: str,
    hashtags: list[str],
) -> str:
    if not cfg.ig_user_id or not cfg.ig_access_token:
        raise InstagramError("IG_USER_ID and IG_ACCESS_TOKEN are required.")
    if len(image_paths) < 2:
        raise InstagramError("Carousel needs at least 2 images.")
    if len(image_paths) > 10:
        raise InstagramError("Carousel supports at most 10 images.")

    image_urls = await upload_images(cfg, image_paths)
    token = cfg.ig_access_token
    media_url = f"{GRAPH}/{cfg.ig_user_id}/media"
    publish_url = f"{GRAPH}/{cfg.ig_user_id}/media_publish"
    caption_text = _caption(caption, hashtags)

    async with httpx.AsyncClient(timeout=60.0) as client:
        child_ids: list[str] = []
        for url in image_urls:
            created = await _request(
                client,
                "POST",
                media_url,
                data={
                    "image_url": url,
                    "is_carousel_item": "true",
                    "access_token": token,
                },
            )
            child_id = created["id"]
            await _wait_finished(client, child_id, token)
            child_ids.append(child_id)

        parent_data = {
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption_text,
            "is_ai_generated": "true",
            "access_token": token,
        }
        try:
            parent = await _request(client, "POST", media_url, data=parent_data)
        except InstagramError:
            parent_data.pop("is_ai_generated", None)
            parent = await _request(client, "POST", media_url, data=parent_data)
        parent_id = parent["id"]
        await _wait_finished(client, parent_id, token)

        published = await _request(
            client,
            "POST",
            publish_url,
            data={"creation_id": parent_id, "access_token": token},
        )
    return str(published["id"])


async def publish_reel(
    cfg: AppConfig,
    video_path: Path,
    *,
    caption: str,
    hashtags: list[str],
) -> str:
    if not cfg.ig_user_id or not cfg.ig_access_token:
        raise InstagramError("IG_USER_ID and IG_ACCESS_TOKEN are required.")
    path = Path(video_path)
    if not path.exists():
        raise InstagramError(f"Reel video not found: {path}")

    token = cfg.ig_access_token
    media_url = f"{GRAPH}/{cfg.ig_user_id}/media"
    publish_url = f"{GRAPH}/{cfg.ig_user_id}/media_publish"
    size = path.stat().st_size
    caption_text = _caption(caption, hashtags)

    async with httpx.AsyncClient(timeout=300.0) as client:
        session_data = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption_text,
            "share_to_feed": "true",
            "is_ai_generated": "true",
            "access_token": token,
        }
        try:
            session = await _request(client, "POST", media_url, data=session_data)
        except InstagramError:
            session_data.pop("is_ai_generated", None)
            session = await _request(client, "POST", media_url, data=session_data)

        container_id = session["id"]
        upload_uri = session.get("uri") or (
            f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}/{container_id}"
        )
        upload = await client.post(
            upload_uri,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
                "Content-Type": "application/octet-stream",
            },
            content=path.read_bytes(),
        )
        if not upload.is_success:
            raise InstagramError(f"Reel upload failed: {upload.text}")

        await _wait_finished(client, container_id, token, attempts=180)
        published = await _request(
            client,
            "POST",
            publish_url,
            data={"creation_id": container_id, "access_token": token},
        )
    return str(published["id"])


def publish_carousel_sync(
    cfg: AppConfig,
    image_paths: list[Path],
    *,
    caption: str,
    hashtags: list[str],
) -> str:
    return asyncio.run(
        publish_carousel(cfg, image_paths, caption=caption, hashtags=hashtags)
    )


def publish_reel_sync(
    cfg: AppConfig,
    video_path: Path,
    *,
    caption: str,
    hashtags: list[str],
) -> str:
    return asyncio.run(
        publish_reel(cfg, video_path, caption=caption, hashtags=hashtags)
    )
