# -*- coding: utf-8 -*-
"""
매일 아침 실행되는 종목 스크리닝 메인 스크립트.

시장(코스피/코스닥)을 나눠서, 아래 리스트를 "따로" 뽑는다 (동시 만족 필요 없음):
1) RSI(14) 30 이하 종목 중 가장 과매도인 상위 10개
2) 전일 외국인+기관 순매수 합계 상위 10개
3) 이격도(20일선)가 가장 낮은 하위 10개
4) 거래대금(종가×거래량) 상위 10개
5) 거래량 급증배수(당일거래량 / 20일평균거래량) 상위 10개
6) 정배열(5일선 > 20일선 > 60일선) 종목 중 단기/장기 이평 격차가 가장 큰 상위 10개

그리고 "이격도/RSI/거래대금/거래량급증배수/정배열" 5개 지표 중
- 3개 이상 동시에 겹치는 종목
- 2개 이상 동시에 겹치는 종목
을 각각 별도로 표시한다. (순매수는 이 중복 판정에는 포함하지 않음)

* KRX(pykrx)는 GitHub Actions 등 해외 서버에서 접속이 차단되는 경우가 있어
  사용하지 않고, 시세/수급/종목목록 모두 네이버 증권에서 가져온다.

결과: docs/results.json 에 저장 (GitHub Pages가 이 폴더를 서빙)
또한, "2개 이상 중복" 종목들을 docs/history.json에 매일 누적 기록해서
나중에 사후 검증(evaluate_picks.py)에 사용한다.
"""
import os
import json
import datetime
import traceback

import pandas as pd

from indicators import calc_rsi, calc_disparity, calc_ma
from naver_price import get_daily_ohlcv
from naver_investor import get_foreign_institution_net
from naver_universe import get_market_universe
from retry_util import retry_call

# ----------------------------------------------------------------------------
# 설정값 (필요시 조정)
# ----------------------------------------------------------------------------
RSI_THRESHOLD = 30
RSI_TOP_N = 10
NET_BUY_TOP_N = 10
DISPARITY_MA_PERIOD = 20
DISPARITY_TOP_N = 10
TURNOVER_TOP_N = 10
VOLUME_SURGE_TOP_N = 10
VOLUME_SURGE_MA_PERIOD = 20
MA_SHORT = 5
MA_MID = 20
MA_LONG = 60
MA_ALIGN_TOP_N = 10
PRICE_HISTORY_DAYS = 70      # 60일선 계산을 위해 넉넉히 확보
INVESTOR_HISTORY_DAYS = 5
TOP_N_PER_MARKET = 150
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.json")
HISTORY_MAX_ENTRIES = 200    # 누적 기록 최대 보관 일수 (너무 커지지 않도록 제한)

MARKETS = {"kospi": 0, "kosdaq": 1}

# 중복(overlap) 판정에 사용할 5개 지표 (순매수는 제외)
OVERLAP_TAGS = ["rsi", "disparity", "turnover", "volume_surge", "ma_align"]


def get_label_date() -> str:
    d = datetime.date.today()
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def build_record(code: str, name: str) -> dict | None:
    """종목 하나의 RSI/이격도/거래대금/거래량급증/정배열/전일수급 데이터를 모아 반환."""
    try:
        price_df = get_daily_ohlcv(code, days=PRICE_HISTORY_DAYS)
        min_required = max(DISPARITY_MA_PERIOD, MA_LONG) + 3
        if len(price_df) < min_required:
            return None

        close = price_df["close"]
        volume = price_df["volume"]

        rsi = calc_rsi(close, period=14)
        disparity = calc_disparity(close, ma_period=DISPARITY_MA_PERIOD)
        ma_short = calc_ma(close, MA_SHORT)
        ma_mid = calc_ma(close, MA_MID)
        ma_long = calc_ma(close, MA_LONG)

        latest_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else None
        change_pct = (
            round((latest_close - prev_close) / prev_close * 100, 2)
            if prev_close
            else None
        )

        latest_rsi = rsi.iloc[-1]
        latest_disparity = disparity.iloc[-1]
        latest_ma_short = ma_short.iloc[-1]
        latest_ma_mid = ma_mid.iloc[-1]
        latest_ma_long = ma_long.iloc[-1]

        if pd.isna(latest_rsi) or pd.isna(latest_disparity) or pd.isna(latest_ma_long):
            return None

        latest_volume = float(volume.iloc[-1])
        avg_volume = float(volume.tail(VOLUME_SURGE_MA_PERIOD).mean())
        volume_surge = (latest_volume / avg_volume) if avg_volume > 0 else 0.0
        turnover = latest_close * latest_volume

        ma_aligned = bool(latest_ma_short > latest_ma_mid > latest_ma_long)
        ma_strength = (
            round((latest_ma_short / latest_ma_long - 1) * 100, 2)
            if latest_ma_long > 0
            else None
        )

        investor_df = get_foreign_institution_net(code, days=INVESTOR_HISTORY_DAYS)
        if investor_df.empty:
            net_buy_1d = 0
        else:
            last_row = investor_df.iloc[-1]
            net_buy_1d = int(last_row["foreign_net"] + last_row["institution_net"])

        return {
            "code": code,
            "name": name,
            "close": latest_close,
            "change_pct": change_pct,
            "rsi": round(float(latest_rsi), 2),
            "disparity": round(float(latest_disparity), 2),
            "net_buy_1d": net_buy_1d,
            "turnover": turnover,
            "volume_surge": round(volume_surge, 2),
            "ma_aligned": ma_aligned,
            "ma_strength": ma_strength,
        }
    except Exception:
        print(f"[WARN] {code} 처리 중 오류:\n{traceback.format_exc()}")
        return None


def screen_market(sosok: int, market_label: str) -> dict:
    stock_list = retry_call(get_market_universe, sosok, TOP_N_PER_MARKET, retries=3, delay=3.0)
    print(f"[{market_label}] 스캔 대상: {len(stock_list)}개")

    records = []
    for idx, item in enumerate(stock_list, start=1):
        rec = build_record(item["code"], item["name"])
        if rec:
            records.append(rec)
        if idx % 50 == 0:
            print(f"[{market_label}] 진행 상황: {idx}/{len(stock_list)}")

    rsi_candidates = [r for r in records if r["rsi"] <= RSI_THRESHOLD]
    rsi_list = sorted(rsi_candidates, key=lambda r: r["rsi"])[:RSI_TOP_N]

    net_buy_list = sorted(records, key=lambda r: r["net_buy_1d"], reverse=True)[:NET_BUY_TOP_N]

    disparity_list = sorted(records, key=lambda r: r["disparity"])[:DISPARITY_TOP_N]

    turnover_list = sorted(records, key=lambda r: r["turnover"], reverse=True)[:TURNOVER_TOP_N]

    volume_surge_list = sorted(records, key=lambda r: r["volume_surge"], reverse=True)[:VOLUME_SURGE_TOP_N]

    ma_align_candidates = [r for r in records if r["ma_aligned"]]
    ma_align_list = sorted(
        ma_align_candidates, key=lambda r: (r["ma_strength"] or 0), reverse=True
    )[:MA_ALIGN_TOP_N]

    list_by_tag = {
        "rsi": rsi_list,
        "disparity": disparity_list,
        "turnover": turnover_list,
        "volume_surge": volume_surge_list,
        "ma_align": ma_align_list,
    }

    tag_map = {}
    for tag in OVERLAP_TAGS:
        for r in list_by_tag[tag]:
            tag_map.setdefault(r["code"], set()).add(tag)

    record_by_code = {r["code"]: r for r in records}

    def build_overlap(min_count: int):
        out = []
        for code, tags in tag_map.items():
            if len(tags) >= min_count:
                item = dict(record_by_code[code])
                item["matched_lists"] = sorted(tags)
                out.append(item)
        out.sort(key=lambda r: (-len(r["matched_lists"]), r["rsi"]))
        return out

    overlap_3plus = build_overlap(3)
    overlap_2plus = build_overlap(2)

    return {
        "scanned_count": len(records),
        "overlap_3plus": overlap_3plus,
        "overlap_2plus": overlap_2plus,
        "rsi_low": rsi_list,
        "net_buy_top": net_buy_list,
        "disparity_low": disparity_list,
        "turnover_top": turnover_list,
        "volume_surge_top": volume_surge_list,
        "ma_align_top": ma_align_list,
    }


def append_history(base_date: str, result: dict):
    """
    "2개 이상 중복" 종목들을 매일 docs/history.json에 누적 기록.
    (전체 리스트를 다 저장하면 파일이 너무 커지므로, 사후 검증에 필요한 최소 정보만 저장)
    """
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            history = {"entries": []}
    else:
        history = {"entries": []}

    entries = history.get("entries", [])

    # 같은 날짜가 이미 있으면 덮어쓰기(중복 방지)
    entries = [e for e in entries if e.get("date") != base_date]

    def slim(items):
        return [
            {
                "code": it["code"],
                "name": it["name"],
                "close": it["close"],
                "matched_lists": it.get("matched_lists", []),
            }
            for it in items
        ]

    entries.append(
        {
            "date": base_date,
            "kospi": slim(result["kospi"]["overlap_2plus"]),
            "kosdaq": slim(result["kosdaq"]["overlap_2plus"]),
        }
    )

    entries.sort(key=lambda e: e["date"])
    entries = entries[-HISTORY_MAX_ENTRIES:]

    history["entries"] = entries
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print(f"히스토리 기록 완료 (총 {len(entries)}일치) -> {HISTORY_PATH}")


def main():
    base_date = get_label_date()
    print(f"기준일(라벨): {base_date}")

    result = {
        "base_date": base_date,
        "generated_at": datetime.datetime.now().isoformat(),
        "conditions": {
            "rsi_threshold": RSI_THRESHOLD,
            "rsi_top_n": RSI_TOP_N,
            "net_buy_top_n": NET_BUY_TOP_N,
            "disparity_ma_period": DISPARITY_MA_PERIOD,
            "disparity_top_n": DISPARITY_TOP_N,
            "turnover_top_n": TURNOVER_TOP_N,
            "volume_surge_top_n": VOLUME_SURGE_TOP_N,
            "ma_periods": [MA_SHORT, MA_MID, MA_LONG],
            "ma_align_top_n": MA_ALIGN_TOP_N,
            "overlap_tags": OVERLAP_TAGS,
            "data_source": "naver",
        },
    }

    for market_label, sosok in MARKETS.items():
        result[market_label] = screen_market(sosok, market_label)
        m = result[market_label]
        print(
            f"[{market_label}] RSI:{len(m['rsi_low'])} 순매수:{len(m['net_buy_top'])} "
            f"이격도:{len(m['disparity_low'])} 거래대금:{len(m['turnover_top'])} "
            f"거래량급증:{len(m['volume_surge_top'])} 정배열:{len(m['ma_align_top'])} "
            f"중복3+:{len(m['overlap_3plus'])} 중복2+:{len(m['overlap_2plus'])}"
        )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"완료 -> {OUTPUT_PATH}")

    append_history(base_date, result)

    return result


if __name__ == "__main__":
    main()
