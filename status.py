#!/usr/bin/env python3
"""Readable, read-only status of the local OpenAI daily free-token gateway.

No API key or dependency is needed. This never sends a model request.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from gate import BIG_MODELS, SMALL_MODELS


STATUS_URL = "http://127.0.0.1:18765/status"
OFFICIAL_LIMITS = {"big": 250_000, "small": 2_500_000}
KST = timezone(timedelta(hours=9))

# Only the nine model names shown in Mixdog, not an assertion about every
# OpenAI model available to the user's organization.
DISPLAY_MODELS = (
    ("GPT-6 Astra", "gpt-6-astra"),
    ("GPT-6.1 Sol", "gpt-6.1-sol"),
    ("GPT-6 Sol", "gpt-6-sol"),
    ("GPT-6 Luna", "gpt-6-luna"),
    ("GPT-5.6 Sol", "gpt-5.6-sol"),
    ("GPT-5.5", "gpt-5.5-2026-04-23"),
    ("o3", "o3-2025-04-16"),
    ("GPT-5.6 Terra", "gpt-5.6-terra"),
    ("GPT-5.6 Luna", "gpt-5.6-luna"),
)


def model_group(model_id):
    if model_id in BIG_MODELS:
        return "big"
    if model_id in SMALL_MODELS:
        return "small"
    return None


def reset_time(now=None):
    now = now or datetime.now(KST)
    t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= t:
        t += timedelta(days=1)
    return t


def bar(used, cap, width=24):
    if not cap:
        return "-" * width
    filled = min(width, max(0, round(width * used / cap)))
    return "#" * filled + "-" * (width - filled)


def fmt(n):
    return f"{int(n):,}"


def fetch_status(url=STATUS_URL):
    with urlopen(url, timeout=3) as response:
        data = json.load(response)
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise ValueError("상태 응답의 형식이 예상과 다릅니다.")
    return data


def render(data, show_models=True):
    now = datetime.now(KST)
    until = reset_time(now) - now
    hours, rem = divmod(int(until.total_seconds()), 3600)
    minutes = rem // 60
    lines = [
        "OpenAI 일일 무료 토큰 차단기",
        "=" * 45,
        f"조회: {now:%Y-%m-%d %H:%M:%S} KST",
        f"장부 날짜: {data.get('utc_day', '(알 수 없음)')} (UTC)",
    ]
    groups = data["groups"]
    for key, title in (("big", "25만 토큰 그룹"), ("small", "250만 토큰 그룹")):
        values = groups.get(key, {})
        used = max(0, int(values.get("local_used", 0)))
        held = max(0, int(values.get("in_flight_reserved", 0)))
        cap = max(0, int(values.get("local_cap", 0)))
        available = max(0, int(values.get("available_for_reservation", 0)))
        lines += [
            "",
            f"[{title}]",
            f"  사용: {fmt(used)} / 차단기 상한: {fmt(cap)} 토큰",
            f"  남음: {fmt(available)} 토큰"
            + (f" (진행 중 예약 {fmt(held)} 포함)" if held else ""),
            f"  [{bar(used + held, cap)}] "
            + (f"{(used + held) / cap * 100:.2f}%" if cap else "?"),
            f"  공식 무료 한도: {fmt(OFFICIAL_LIMITS[key])} 토큰",
        ]
    lines += [
        "",
        f"다음 초기화: {reset_time(now):%m월 %d일 %H:%M} KST "
        f"(약 {hours}시간 {minutes}분 후)",
        "",
        "※ 차단기를 거친 요청만 기록됩니다. 조직 전체의 공식 잔액과 다를 수 있습니다.",
    ]
    if show_models:
        lines += ["", "표시 모델의 그룹 분류:"]
        for group, label in (("big", "25만"), ("small", "250만")):
            names = [name for name, model in DISPLAY_MODELS if model_group(model) == group]
            lines.append(f"  [{label}] " + ", ".join(names))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="무료 토큰 그룹별 남은 로컬 사용량 표시")
    parser.add_argument(
        "--watch", nargs="?", const=5, type=float, metavar="SECONDS",
        help="주기적으로 새로 조회 (기본 5초, Ctrl+C로 종료)",
    )
    parser.add_argument(
        "--no-models", action="store_true",
        help="모델 그룹 목록은 숨기고 잔량만 표시",
    )
    args = parser.parse_args(argv)
    if args.watch is not None and args.watch <= 0:
        parser.error("--watch 주기는 0보다 커야 합니다.")

    while True:
        try:
            data = fetch_status()
            output = render(data, show_models=not args.no_models)
        except (URLError, TimeoutError, ValueError, OSError) as error:
            output = (
                "차단기 상태를 조회하지 못했습니다. 서비스가 실행 중인지 확인해 주세요.\n"
                "  systemctl --user status openai-daily-free-gate\n"
                f"  ({error})"
            )
            if args.watch is None:
                print(output, file=sys.stderr)
                return 1
        if args.watch is not None and sys.stdout.isatty():
            print("\033[2J\033[H", end="")
        print(output, flush=True)
        if args.watch is None:
            return 0
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n상태 감시 종료.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
