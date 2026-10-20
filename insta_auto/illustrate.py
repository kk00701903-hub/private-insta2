from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from insta_auto.analyze import AnalysisResult
from insta_auto.config import AppConfig
from insta_auto import kling_cli


class IllustrateError(RuntimeError):
    pass


def _auth_header(cfg: AppConfig) -> dict[str, str]:
    ak = cfg.kling_api_key
    sk = cfg.kling_secret_key
    if not ak:
        raise IllustrateError("KLING_API_KEY is not set")
    if ak and sk:
        try:
            import jwt

            now = int(time.time())
            token = jwt.encode(
                {"iss": ak, "exp": now + 1800, "nbf": now - 5},
                sk,
                algorithm="HS256",
                headers={"alg": "HS256", "typ": "JWT"},
            )
            return {"Authorization": f"Bearer {token}"}
        except Exception:
            pass
    return {"Authorization": f"Bearer {ak}"}


async def _request(
    cfg: AppConfig,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", **_auth_header(cfg)}
    url = f"{cfg.kling_base_url}{path}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(method, url, headers=headers, json=json)
        if resp.status_code >= 400:
            raise IllustrateError(f"Kling API {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}


def _task_id(payload: dict[str, Any]) -> str:
    data = payload.get("data") or payload
    if isinstance(data, dict):
        for key in ("task_id", "taskId", "id"):
            value = data.get(key)
            if value:
                return str(value)
    raise IllustrateError(f"Kling task id not found: {payload!r}")


def _result_urls(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") or payload
    urls: list[str] = []
    if not isinstance(data, dict):
        return urls
    task_result = data.get("task_result") or data.get("taskResult") or data
    if isinstance(task_result, dict):
        block = task_result.get("images") or task_result.get("image")
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict):
                    url = item.get("url") or item.get("image_url")
                    if url:
                        urls.append(str(url))
                elif isinstance(item, str):
                    urls.append(item)
        elif isinstance(block, dict):
            url = block.get("url") or block.get("image_url")
            if url:
                urls.append(str(url))
        elif isinstance(block, str):
            urls.append(block)
    return urls


async def _poll(cfg: AppConfig, task_id: str, *, attempts: int = 60) -> list[str]:
    for _ in range(attempts):
        payload = await _request(cfg, "GET", f"/v1/images/generations/{task_id}")
        data = payload.get("data") or payload
        status = ""
        if isinstance(data, dict):
            status = str(data.get("task_status") or data.get("status") or "").upper()
        if status in {"SUCCEED", "SUCCESS", "SUCCEEDED", "FINISHED"}:
            return _result_urls(payload)
        if status in {"FAILED", "ERROR"}:
            raise IllustrateError(f"Kling task failed: {payload}")
        await asyncio.sleep(2)
    raise IllustrateError(f"Timed out waiting for Kling task {task_id}")


async def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


def _want_cli(cfg: AppConfig) -> bool:
    backend = (cfg.kling_backend or "auto").lower()
    if backend == "cli":
        return True
    if backend == "api":
        return False
    # auto: CLI if installed
    return kling_cli.cli_available(cfg)


def _want_api(cfg: AppConfig) -> bool:
    backend = (cfg.kling_backend or "auto").lower()
    if backend == "api":
        return bool(cfg.kling_api_key)
    if backend == "cli":
        return False
    return bool(cfg.kling_api_key)


def kling_ready(cfg: AppConfig) -> dict[str, Any]:
    """Status for UI /api/state."""
    cli = kling_cli.login_status(cfg) if kling_cli.cli_available(cfg) else {
        "available": False,
        "logged_in": False,
        "models": [],
        "error": "CLI not installed",
    }
    return {
        "backend": cfg.kling_backend,
        "cli": cli,
        "has_api_key": bool(cfg.kling_api_key),
        "ready": bool(cli.get("logged_in") or cfg.kling_api_key),
        "model": cfg.kling_image_model
        or ((cli.get("models") or [None])[0]),
    }


async def _generate_via_cli(prompt: str, dest: Path, cfg: AppConfig) -> Path:
    return await asyncio.to_thread(
        kling_cli.text_to_image,
        prompt,
        dest,
        cfg=cfg,
    )


async def _generate_via_api(prompt: str, dest: Path, cfg: AppConfig) -> Path:
    created = await _request(
        cfg,
        "POST",
        "/v1/images/generations",
        json={
            "model_name": cfg.kling_image_model or "kling-v1",
            "prompt": prompt,
            "n": 1,
            "aspect_ratio": "9:16" if cfg.kling_aspect_ratio == "9:16" else "3:4",
        },
    )
    task_id = _task_id(created)
    urls = await _poll(cfg, task_id)
    if not urls:
        raise IllustrateError("Kling API returned no image URL")
    return await _download(urls[0], dest)


async def text_to_image(prompt: str, dest: Path, *, cfg: AppConfig) -> Path:
    """Generate illustration: CLI first (auto/cli), then REST API fallback."""
    errors: list[str] = []
    if _want_cli(cfg):
        try:
            return await _generate_via_cli(prompt, dest, cfg)
        except Exception as exc:
            errors.append(f"cli: {exc}")
            if (cfg.kling_backend or "auto").lower() == "cli":
                raise IllustrateError(str(exc)) from exc
    if _want_api(cfg):
        try:
            return await _generate_via_api(prompt, dest, cfg)
        except Exception as exc:
            errors.append(f"api: {exc}")
            raise IllustrateError(str(exc)) from exc
    detail = "; ".join(errors) if errors else "no Kling backend configured"
    raise IllustrateError(
        f"Kling unavailable ({detail}). "
        "Run `npm run kling:login` or set KLING_API_KEY."
    )


async def generate_illustrations(
    analysis: AnalysisResult,
    out_dir: Path,
    *,
    cfg: AppConfig,
) -> dict[int, Path]:
    """Generate illustrations for slides that need them. Returns slide_index -> path.

    Skips entirely when is_real_place or no Kling (CLI login / API key).
    """
    if analysis.is_real_place:
        return {}

    status = kling_ready(cfg)
    if not status["ready"]:
        return {}

    results: dict[int, Path] = {}
    for i, slide in enumerate(analysis.slides):
        if not slide.need_illustration:
            continue
        prompt = (slide.illustration_prompt or slide.title or analysis.topic).strip()
        if not prompt:
            continue
        full_prompt = (
            f"{prompt}. Clean illustration, no text, no watermark, "
            "no distorted hands or faces."
        )
        dest = out_dir / f"illus_{i + 1:02d}.png"
        try:
            await text_to_image(full_prompt, dest, cfg=cfg)
            results[i] = dest
        except IllustrateError:
            continue
    return results


def generate_illustrations_sync(
    analysis: AnalysisResult,
    out_dir: Path,
    *,
    cfg: AppConfig,
) -> dict[int, Path]:
    return asyncio.run(generate_illustrations(analysis, out_dir, cfg=cfg))
