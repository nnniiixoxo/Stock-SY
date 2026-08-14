# -*- coding: utf-8 -*-
"""
사후 검증 스크립트.

main.py가 매일 docs/history.json에 쌓아둔 "2개 이상 중복" 픽들 중,
- 픽 시점으로부터 7일(달력 기준, 약 1주일) 이상 지났고
- 아직 1주일 성과를 평가하지 않은 픽
- 픽 시점으로부터 30일(달력 기준, 약 1개월) 이상 지났고
- 아직 1개월 성과를 평가하지 않은 픽
들에 대해 현재가를 조회해서 수익률을 계산하고, 그 결과를 history.json에 채워 넣는다.
그리고 전체 통계(평균 수익률, 승률)를 docs/performance_summary.json에 저장한다.

달력일 기준으로 평가하는 이유: 실제 거래일 캘린더(공휴일 등) 없이도 안정적으로
동작하게 하기 위함. "1주일 후", "1개월 후"라는 표현은 이 의미로 사용한다.
"""
import os
import json
import datetime

from naver_price import get_daily_ohlcv

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.json")
SUMMARY_PATH = os.path.join(os.path.dirname(__file__), "docs", "performance_summary.json")

HORIZONS = {"1w": 7, "1m": 30}  # 평가 시점 이름 -> 픽 이후 경과 일수(달력 기준)

_price_cache = {}


def get_current_close(code: str):
    """캐시를 사용해 종목의 최신 종가를 조회 (같은 실행 내에서 중복 조회 방지)."""
    if code in _price_cache:
        return _price_cache[code]
    try:
        df = get_daily_ohlcv(code, days=5)
        price = float(df["close"].iloc[-1]) if not df.empty else None
    except Exception:
        price = None
    _price_cache[code] = price
    return price


def evaluate_entries(history: dict) -> dict:
    today = datetime.date.today()
    entries = history.get("entries", [])
    updated_count = 0

    for entry in entries:
        try:
            entry_date = datetime.datetime.strptime(entry["date"], "%Y%m%d").date()
        except (KeyError, ValueError):
            continue
        elapsed_days = (today - entry_date).days

        for market in ("kospi", "kosdaq"):
            for pick in entry.get(market, []):
                pick.setdefault("eval", {})
                for horizon_key, horizon_days in HORIZONS.items():
                    if horizon_key in pick["eval"]:
                        continue  # 이미 평가됨
                    if elapsed_days < horizon_days:
                        continue  # 아직 평가 시점이 안 됨

                    current_price = get_current_close(pick["code"])
                    if current_price is None or not pick.get("close"):
                        continue

                    return_pct = round(
                        (current_price - pick["close"]) / pick["close"] * 100, 2
                    )
                    pick["eval"][horizon_key] = {
                        "return_pct": return_pct,
                        "evaluated_at": today.strftime("%Y%m%d"),
                    }
                    updated_count += 1

    print(f"새로 평가한 픽 개수: {updated_count}")
    return history


def build_summary(history: dict) -> dict:
    stats = {}
    for market in ("kospi", "kosdaq"):
        stats[market] = {}
        for horizon_key in HORIZONS:
            returns = []
            for entry in history.get("entries", []):
                for pick in entry.get(market, []):
                    ev = pick.get("eval", {}).get(horizon_key)
                    if ev is not None:
                        returns.append(ev["return_pct"])
            if returns:
                avg_return = round(sum(returns) / len(returns), 2)
                win_rate = round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1)
                stats[market][horizon_key] = {
                    "avg_return": avg_return,
                    "win_rate": win_rate,
                    "count": len(returns),
                }
            else:
                stats[market][horizon_key] = {
                    "avg_return": None,
                    "win_rate": None,
                    "count": 0,
                }
    return {
        "updated_at": datetime.datetime.now().isoformat(),
        **stats,
    }


def main():
    if not os.path.exists(HISTORY_PATH):
        print("history.json이 아직 없습니다. main.py를 먼저 실행해 데이터를 쌓아주세요.")
        return

    with open(HISTORY_PATH, encoding="utf-8") as f:
        history = json.load(f)

    history = evaluate_entries(history)

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    summary = build_summary(history)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"완료 -> {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
