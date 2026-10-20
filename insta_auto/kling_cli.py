"""Official Kling CLI wrapper (`@klingai/cli-global` → `kling`).

Prefer this over raw REST. Login: `npm run kling:login` (browser OAuth).
Stdout is JSON; use --quiet for compact single-line output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from insta_auto.config import ROOT, AppConfig

DEFAULT_POLL = 120
DEFAULT_ASPECT = "3:4"  # close to 4:5 cards; CLI models often use 3:4 / 9:16


class KlingCliError(RuntimeError):
    pass


def _node_bin() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")


def resolve_kling_cmd(cfg: AppConfig | None = None) -> list[str]:
    """Return argv prefix to invoke Kling CLI."""
    if cfg and cfg.kling_cli:
        custom = Path(cfg.kling_cli)
        if custom.exists():
            return [str(custom)]
        # allow "kling" or full command string split
        parts = str(cfg.kling_cli).split()
        if parts:
            return parts

    env_cli = (os.getenv("KLING_CLI") or "").strip()
    if env_cli:
        p = Path(env_cli)
        if p.exists():
            return [str(p)]
        return env_cli.split()

    which = shutil.which("kling") or shutil.which("kling.cmd")
    if which:
        return [which]

    local = ROOT / "node_modules" / "@klingai" / "cli-global"
    # package bin entry
    candidates = [
        ROOT / "node_modules" / ".bin" / "kling.cmd",
        ROOT / "node_modules" / ".bin" / "kling",
    ]
    for c in candidates:
        if c.exists():
            return [str(c)]

    # npx fallback from project root
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if npx and local.exists():
        return [npx, "--no-install", "kling"]

    raise KlingCliError(
        "Kling CLI를 찾을 수 없습니다. `npm install` 후 `npm run kling:login` 하세요."
    )


def cli_available(cfg: AppConfig | None = None) -> bool:
    try:
        resolve_kling_cmd(cfg)
        return True
    except KlingCliError:
        return False


def run_kling(
    args: list[str],
    *,
    cfg: AppConfig | None = None,
    timeout: int = 360,
) -> dict[str, Any] | list[Any] | str:
    cmd = [*resolve_kling_cmd(cfg), "--quiet", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise KlingCliError(f"Kling CLI timed out: {' '.join(args)}") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        msg = stderr or stdout or f"exit {proc.returncode}"
        raise KlingCliError(msg)

    if not stdout:
        return {}
    # Prefer last JSON object/array line
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        if line[0] in "{[":
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return stdout


def login_status(cfg: AppConfig | None = None) -> dict[str, Any]:
    """Return {available, logged_in, user, models, error}."""
    info: dict[str, Any] = {
        "available": cli_available(cfg),
        "logged_in": False,
        "user": None,
        "models": [],
        "error": None,
    }
    if not info["available"]:
        info["error"] = "CLI not installed"
        return info
    try:
        payload = run_kling(["who_am_i"], cfg=cfg, timeout=60)
        if isinstance(payload, dict):
            info["logged_in"] = True
            info["user"] = payload.get("user") or payload.get("data")
            models = (
                (payload.get("availableModels") or {})
                .get("text_to_image")
                or []
            )
            if isinstance(models, list):
                info["models"] = [
                    m.get("model") for m in models if isinstance(m, dict) and m.get("model")
                ]
        return info
    except KlingCliError as exc:
        err = str(exc)
        info["error"] = err
        if "login" in err.lower() or "ACCESS_TOKEN" in err or "credentials" in err.lower():
            info["logged_in"] = False
        return info


def pick_image_model(cfg: AppConfig) -> str:
    if cfg.kling_image_model:
        return cfg.kling_image_model
    status = login_status(cfg)
    models = status.get("models") or []
    if models:
        return str(models[0])
    # common defaults from Kling docs
    return "kling-image-v3_0"


def _extract_urls(payload: Any) -> list[str]:
    urls: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key in (
                "url_without_watermark",
                "urlWithoutWatermark",
                "url",
                "image_url",
                "imageUrl",
            ):
                val = obj.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    urls.append(val)
                    return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def text_to_image(
    prompt: str,
    dest: Path,
    *,
    cfg: AppConfig,
    aspect_ratio: str | None = None,
    poll: int | None = None,
) -> Path:
    """Generate one image via Kling CLI and download to dest."""
    import httpx

    model = pick_image_model(cfg)
    aspect = aspect_ratio or cfg.kling_aspect_ratio or DEFAULT_ASPECT
    poll_s = poll if poll is not None else cfg.kling_poll_seconds or DEFAULT_POLL

    args = [
        "text_to_image",
        "--model",
        model,
        "--aspectRatio",
        aspect,
        "--imageCount",
        "1",
        "--poll",
        str(poll_s),
        prompt,
    ]
    payload = run_kling(args, cfg=cfg, timeout=poll_s + 90)
    urls = _extract_urls(payload)
    if not urls:
        raise KlingCliError(f"No image URL in Kling CLI response: {payload!r}"[:800])

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        resp = client.get(urls[0])
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest
