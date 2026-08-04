# -*- coding: utf-8 -*-
"""
(선택 기능) 스크리닝 결과를 텔레그램 봇 메시지로 발송.
아이폰에 텔레그램 앱이 설치되어 있으면 실제 푸시 알림으로 옵니다.

필요한 환경변수(= GitHub Secrets):
  TELEGRAM_BOT_TOKEN : @BotFather 로 만든 봇의 토큰
  TELEGRAM_CHAT_ID   : 알림 받을 사용자/채팅방 ID

설정 방법은 README.md 참고.
"""
import os
import json
import requests

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")


def build_message(data: dict) -> str:
    lines = [f"📊 {data.get('base_date')} 기준 종목 탐색 결과\n"]
    for market_label, market_name in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        m = data.get(market_label)
        if not m:
            continue
        overlap = m.get("overlap_3plus") or []
        lines.append(f"[{market_name}] 5개 지표 중 3개 이상 중복 {len(overlap)}종목")
        if not overlap:
            lines.append("  없음")
        else:
            for i, s in enumerate(overlap[:10], start=1):
                tags = "+".join(s.get("matched_lists", []))
                lines.append(f"  {i}. {s['name']}({s['code']}) RSI {s['rsi']} · {tags}")
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[알림 스킵] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
    ok = resp.status_code == 200
    if not ok:
        print(f"[알림 실패] {resp.status_code} {resp.text}")
    return ok


if __name__ == "__main__":
    with open(RESULTS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    msg = build_message(data)
    print(msg)
    send_telegram_message(msg)
