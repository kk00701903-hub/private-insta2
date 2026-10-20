from __future__ import annotations

import argparse
import sys
from pathlib import Path

from insta_auto.config import load_config
from insta_auto.pipeline import collect_photos, create_job, publish_job, reject_job
from insta_auto.worklog import append_worklog


def _ask_approval(*, auto_yes: bool, dry_run: bool) -> bool:
    if dry_run:
        print("\n[dry-run] 업로드를 건너뜁니다.")
        return False
    if auto_yes:
        print("\n[--yes] 승인으로 간주하고 업로드합니다.")
        return True
    while True:
        answer = input("\n업로드할까요? [y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print("y 또는 n 으로 답해 주세요.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="사진+코멘트 → 카드뉴스/릴스 제작 → 승인 후 인스타 업로드",
    )
    p.add_argument("--photos", required=True, help="사진 파일 또는 폴더")
    p.add_argument("--comment", required=True, help="주제·핵심·분위기 코멘트")
    p.add_argument(
        "--format",
        choices=("photo", "reel", "both"),
        default="photo",
        dest="fmt",
        help="게시 형식",
    )
    p.add_argument("--count", type=int, default=None, help="카드 장수")
    p.add_argument("--seconds", type=float, default=None, help="릴스 장당 초")
    p.add_argument("--music", default=None, help="음악 파일 경로 (릴스)")
    p.add_argument("--mood", default="", help="원본과 다르게 바꿀 느낌")
    p.add_argument(
        "--offline",
        action="store_true",
        help="Claude API 없이 오프라인 분석 (스모크/테스트)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="제작만 하고 업로드하지 않음",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="미리보기 후 승인 질문 없이 업로드 (주의)",
    )
    return p


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    photos = collect_photos(Path(args.photos))
    music = Path(args.music) if args.music else None

    if args.fmt in {"reel", "both"} and music is None:
        print(
            "안내: 음악 파일이 지정되지 않았습니다. "
            "상업 이용 가능한 음원을 music/에 두고 --music 으로 지정하세요. "
            "이번에는 무음으로 영상을 만듭니다."
        )

    print("[1-4] 제작 중...")
    job = create_job(
        photos,
        args.comment,
        cfg=cfg,
        fmt=args.fmt,
        count=args.count,
        seconds=args.seconds,
        mood=args.mood,
        music=music,
        offline=args.offline,
    )

    print(f"\n===== 미리보기 =====")
    print(f"작업 폴더: {job.out_dir}")
    for p in job.card_paths:
        print(f"  카드: {cfg.output_dir / p}")
    if job.reel_path:
        print(f"영상: {cfg.output_dir / job.reel_path}")
    print("\n캡션:")
    print(job.caption)
    if job.hashtags:
        print("해시태그:", " ".join(f"#{t}" for t in job.hashtags))

    approved = _ask_approval(auto_yes=args.yes, dry_run=args.dry_run)
    if approved:
        print("[5] 업로드 (Graph API)...")
        job = publish_job(job.job_id, cfg=cfg)
        for mid in job.media_ids:
            print(f"  media id: {mid}")
    else:
        if args.dry_run:
            append_worklog(
                job_id=job.job_id,
                topic=job.topic,
                tools=job.tools,
                result_files=[*(cfg.output_dir / p for p in job.card_paths)]
                + ([cfg.output_dir / job.reel_path] if job.reel_path else []),
                media_ids=[],
                notes="dry-run - 업로드 생략",
            )
        else:
            reject_job(job.job_id, cfg=cfg)
        print("[5] 업로드 생략")

    print(f"\n결과: {job.out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
