# -*- coding: utf-8 -*-
"""
과거 데이터로 main.py의 점수제 스크리닝(8개 조건, 12점 만점)이 실제로
매수 후 상승으로 이어졌는지 검증하는 백테스트 스크립트.

main.py와 완전히 동일한 점수 규칙(SCORE_RULES)과 등급 기준을 그대로 재사용해서,
"오늘 스크리닝 로직을 몇 달 전 그 시점에 그대로 돌렸다면 몇 점이 나왔을지"를
날짜별로 재현하고, 등급(관심종목 8점+ / 매수후보 10점+ / 강한매수후보 12점)별로
실제 1주일/1개월 후 수익률을 계산한다.

★ 중요한 한계 (결과를 볼 때 꼭 감안할 것)
- 종목 유니버스(시가총액 상위 N)는 "오늘 기준"이다. 과거 그 시점의 실제 상위 N과는
  다를 수 있다 (생존편향: 지금 잘나가는 종목 위주로 평가하게 되는 쏠림이 있을 수 있음).
- 네이버 증권 스크래핑 특성상 종목 수 x 기간이 커질수록 요청 수가 많아진다.
  워크플로우 수동 실행(workflow_dispatch) 시 top_n / eval_days 값을 조절해서 쓸 것.

실행 결과: docs/backtest_result.json 에 저장 + 로그에 요약 출력.
"""
import os
import json
import datetime
import traceback

import pandas as pd

from indicators import calc_rsi, calc_disparity, calc_ma
from naver_price import get_daily_ohlcv
from naver_universe import get_market_universe
from retry_util import retry_call
from main import (
    SCORE_RULES,
    TIER_STRONG_BUY,
    TIER_BUY_CANDIDATE,
    TIER_WATCH,
    DISPARITY_MA_PERIOD,
    VOLUME_SURGE_MA_PERIOD,
    MA_SHORT,
    MA_MID,
    MA_LONG,
    RSI_UPCROSS_LEVEL,
    RSI_ZONE_LOW,
    RSI_ZONE_HIGH,
    RVOL_THRESHOLD,
)

# ----------------------------------------------------------------------------
# 설정값 (workflow_dispatch 입력이나 환경변수로 덮어쓸 수 있음)
# ----------------------------------------------------------------------------
TOP_N_PER_MARKET = int(os.environ.get("BACKTEST_TOP_N", "40"))
EVAL_TRADING_DAYS = int(os.environ.get("BACKTEST_EVAL_DAYS", "40"))  # 평가할 과거 거래일 수
FORWARD_HORIZONS = {"1w": 5, "1m": 21}  # 거래일 기준 (달력일 아님)
WARMUP_DAYS = max(DISPARITY_MA_PERIOD, VOLUME_SURGE_MA_PERIOD, MA_LONG) + 3
BUFFER_DAYS = 5
HISTORY_DAYS = WARMUP_DAYS + EVAL_TRADING_DAYS + max(FORWARD_HORIZONS.values()) + BUFFER_DAYS

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "backtest_result.json")
MARKETS = {"kospi": 0, "kosdaq": 1}

# 등급별로 나눠서 성과를 볼 점수 기준선
SCORE_THRESHOLDS = {
    "watch_8plus": TIER_WATCH,
    "buy_candidate_10plus": TIER_BUY_CANDIDATE,
    "strong_buy_12": TIER_STRONG_BUY,
}


def build_stock_panel(code: str, name: str, days: int) -> pd.DataFrame | None:
    """종목 하나의 날짜별 지표 시계열(DataFrame)을 만든다. (main.py의 build_price_record를 시계열 버전으로 확장)"""
    try:
        price_df = get_daily_ohlcv(code, days=days)
        if len(price_df) < WARMUP_DAYS + 5:
            return None

        close = price_df["close"]
        volume = price_df["volume"]

        rsi = calc_rsi(close, period=14)
        disparity = calc_disparity(close, ma_period=DISPARITY_MA_PERIOD)
        ma_short = calc_ma(close, MA_SHORT)
        ma_mid = calc_ma(close, MA_MID)
        ma_long = calc_ma(close, MA_LONG)
        avg_volume = volume.rolling(window=VOLUME_SURGE_MA_PERIOD).mean()
        turnover = close * volume
        avg_turnover = turnover.rolling(window=VOLUME_SURGE_MA_PERIOD).mean()

        df = pd.DataFrame(
            {
                "date": price_df["date"].dt.strftime("%Y-%m-%d"),
                "close": close,
                "rsi": rsi,
                "disparity": disparity,
                "ma_short": ma_short,
                "ma_mid": ma_mid,
                "ma_long": ma_long,
            }
        ).reset_index(drop=True)

        # main.py의 8개 조건을 시계열 전체에 대해 벡터화 계산 (로직은 main.build_price_record와 동일)
        df["ma_aligned"] = (df["ma_short"] > df["ma_mid"]) & (df["ma_mid"] > df["ma_long"])
        df["ma_mid_rising"] = df["ma_mid"] > df["ma_mid"].shift(1)
        df["ma_long_rising"] = df["ma_long"] > df["ma_long"].shift(1)
        df["rsi_zone"] = (df["rsi"] >= RSI_ZONE_LOW) & (df["rsi"] <= RSI_ZONE_HIGH)
        df["rsi_upcross"] = (df["rsi"].shift(1) <= RSI_UPCROSS_LEVEL) & (df["rsi"] > RSI_UPCROSS_LEVEL)

        prev_disp = df["disparity"].shift(1)
        prev2_disp = df["disparity"].shift(2)
        df["disparity_bottom_turn"] = (
            (prev_disp <= prev2_disp) & (df["disparity"] > prev_disp) & (prev_disp < 100)
        )

        df["rvol"] = volume / avg_volume.replace(0, pd.NA)
        df["rvol_high"] = df["rvol"] >= RVOL_THRESHOLD
        df["turnover_increase"] = turnover > avg_turnover

        df["code"] = code
        df["name"] = name
        return df
    except Exception:
        print(f"[WARN] {code} 백테스트 데이터 처리 중 오류:\n{traceback.format_exc()}")
        return None


def row_to_record(row) -> dict | None:
    """DataFrame의 한 행(하루치)을 main.SCORE_RULES 기준으로 채점해서 record dict로 변환."""
    if pd.isna(row["rsi"]) or pd.isna(row["disparity"]) or pd.isna(row["ma_long"]):
        return None

    score = 0
    for key, _label, points in SCORE_RULES:
        val = row.get(key)
        if val is True or val == 1:
            score += points

    if score >= TIER_STRONG_BUY:
        tier = "strong_buy"
    elif score >= TIER_BUY_CANDIDATE:
        tier = "buy_candidate"
    elif score >= TIER_WATCH:
        tier = "watch"
    else:
        tier = None

    return {
        "code": row["code"],
        "name": row["name"],
        "close": float(row["close"]),
        "score": score,
        "tier": tier,
    }


def backtest_market(sosok: int, market_label: str) -> dict:
    stock_list = retry_call(get_market_universe, sosok, TOP_N_PER_MARKET, retries=3, delay=3.0)
    print(f"[{market_label}] 백테스트 대상 종목: {len(stock_list)}개, 종목당 {HISTORY_DAYS}거래일치 수집")

    panels = {}
    for idx, item in enumerate(stock_list, start=1):
        df = build_stock_panel(item["code"], item["name"], HISTORY_DAYS)
        if df is not None and len(df) > 0:
            panels[item["code"]] = df
        if idx % 20 == 0:
            print(f"[{market_label}] 데이터 수집 진행: {idx}/{len(stock_list)}")

    print(f"[{market_label}] 유효 데이터 확보 종목: {len(panels)}개")
    if not panels:
        return {"error": "유효한 종목 데이터가 없음"}

    date_counts = {}
    for df in panels.values():
        for d in df["date"]:
            date_counts[d] = date_counts.get(d, 0) + 1
    min_presence = max(1, len(panels) // 2)
    calendar = sorted(d for d, cnt in date_counts.items() if cnt >= min_presence)

    max_horizon = max(FORWARD_HORIZONS.values())
    usable_calendar = calendar[:-max_horizon] if max_horizon > 0 else calendar
    eval_dates = usable_calendar[-EVAL_TRADING_DAYS:]
    print(
        f"[{market_label}] 평가 대상 거래일: {len(eval_dates)}일 ({eval_dates[0]} ~ {eval_dates[-1]})"
        if eval_dates
        else f"[{market_label}] 평가 가능한 거래일 없음"
    )

    date_to_pos = {code: {d: i for i, d in enumerate(df["date"])} for code, df in panels.items()}

    picks_by_tier_horizon = {tier: {h: [] for h in FORWARD_HORIZONS} for tier in SCORE_THRESHOLDS}
    baseline_by_horizon = {h: [] for h in FORWARD_HORIZONS}
    pick_dates_count = {tier: 0 for tier in SCORE_THRESHOLDS}

    for eval_date in eval_dates:
        records = []
        for code, df in panels.items():
            pos = date_to_pos[code].get(eval_date)
            if pos is None:
                continue
            rec = row_to_record(df.iloc[pos])
            if rec:
                records.append(rec)

        if not records:
            continue

        picked_by_tier = {
            tier_name: {r["code"] for r in records if r["score"] >= min_score}
            for tier_name, min_score in SCORE_THRESHOLDS.items()
        }
        for tier_name, codes in picked_by_tier.items():
            if codes:
                pick_dates_count[tier_name] += 1

        for code in [r["code"] for r in records]:
            pos = date_to_pos[code][eval_date]
            df = panels[code]
            base_close = df["close"].iloc[pos]
            if pd.isna(base_close) or base_close == 0:
                continue
            for h_label, h_days in FORWARD_HORIZONS.items():
                fwd_pos = pos + h_days
                if fwd_pos >= len(df):
                    continue
                fwd_close = df["close"].iloc[fwd_pos]
                if pd.isna(fwd_close):
                    continue
                return_pct = (fwd_close / base_close - 1) * 100
                baseline_by_horizon[h_label].append(return_pct)
                for tier_name, codes in picked_by_tier.items():
                    if code in codes:
                        picks_by_tier_horizon[tier_name][h_label].append(return_pct)

    def summarize(returns: list) -> dict:
        if not returns:
            return {"count": 0, "win_rate": None, "avg_return": None, "median_return": None}
        s = pd.Series(returns)
        return {
            "count": len(returns),
            "win_rate": round(float((s > 0).mean() * 100), 1),
            "avg_return": round(float(s.mean()), 2),
            "median_return": round(float(s.median()), 2),
        }

    result = {"eval_days": len(eval_dates), "pick_appeared_days": pick_dates_count, "tiers": {}}
    for tier_name in SCORE_THRESHOLDS:
        tier_result = {}
        for h_label in FORWARD_HORIZONS:
            pick_summary = summarize(picks_by_tier_horizon[tier_name][h_label])
            base_summary = summarize(baseline_by_horizon[h_label])
            edge = (
                round(pick_summary["avg_return"] - base_summary["avg_return"], 2)
                if pick_summary["avg_return"] is not None and base_summary["avg_return"] is not None
                else None
            )
            tier_result[h_label] = {
                "pick": pick_summary,
                "baseline_all_scanned": base_summary,
                "edge_vs_baseline": edge,
            }
        result["tiers"][tier_name] = tier_result
    return result


def main():
    print(
        f"백테스트 설정: 시장당 상위 {TOP_N_PER_MARKET}종목, 평가 거래일 {EVAL_TRADING_DAYS}일, "
        f"종목당 수집 거래일 {HISTORY_DAYS}일, 예측 구간 {FORWARD_HORIZONS}"
    )

    output = {
        "run_at": datetime.datetime.now().isoformat(),
        "config": {
            "top_n_per_market": TOP_N_PER_MARKET,
            "eval_trading_days": EVAL_TRADING_DAYS,
            "forward_horizons_trading_days": FORWARD_HORIZONS,
            "score_thresholds": SCORE_THRESHOLDS,
        },
        "caveats": [
            "종목 유니버스는 오늘 기준 시가총액 상위 N (과거 시점 실제 상위 N과 다를 수 있음, 생존편향 가능)",
            "baseline_all_scanned은 그 날 스캔된 전체 종목의 동일 기간 평균 수익률 (시장 전체 상승/하락 흐름 대비 비교용)",
        ],
    }

    for market_label, sosok in MARKETS.items():
        print(f"\n===== {market_label.upper()} 백테스트 시작 =====")
        output[market_label] = backtest_market(sosok, market_label)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("\n\n===================== 백테스트 결과 요약 =====================")
    for market_label in MARKETS:
        m = output[market_label]
        if "error" in m:
            print(f"[{market_label}] {m['error']}")
            continue
        print(f"\n[{market_label.upper()}] 평가 거래일 {m['eval_days']}일")
        for tier_name, tier_result in m["tiers"].items():
            appeared = m["pick_appeared_days"][tier_name]
            print(f"  --- {tier_name} (등장일 {appeared}일) ---")
            for h_label, h in tier_result.items():
                p, b = h["pick"], h["baseline_all_scanned"]
                print(
                    f"    {h_label} 후: 픽 {p['count']}건 (승률 {p['win_rate']}%, 평균 {p['avg_return']}%) "
                    f"vs 전체 스캔 평균 {b['avg_return']}%  ->  차이(엣지) {h['edge_vs_baseline']}%p"
                )
    print(f"\n완료 -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
