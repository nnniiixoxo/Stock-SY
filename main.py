# -*- coding: utf-8 -*-
"""
매일 아침 실행되는 종목 스크리닝 메인 스크립트.

시장(코스피/코스닥)을 나눠서, 아래 리스트를 "따로" 뽑는다 (동시 만족 필요 없음):
1) RSI(14) 30 이하 종목 중 가장 과매도인 상위 10개
2) 전일 외국인+기관 순매수 합계 상위 10개
3) 이격도(20일선)가 가장 낮은 하위 10개
4) 거래대금(종가×거래량) 상위 10개
5) 거래량 급증배수(당일거래량 / 20일평균거래량) 상위 10개
6) 52주 신고가 근접도(종가/52주최고 비율) 상위 10개

그리고 "이격도/RSI/거래대금/거래량급증배수/52주신고가근접도" 5개 지표 중
- 3개 이상 동시에 겹치는 종목
- 2개 이상 동시에 겹치는 종목
을 각각 별도로 표시한다. (순매수는 이 중복 판정에는 포함하지 않음)

* KRX(pykrx)는 GitHub Actions 등 해외 서버에서 접속이 차단되는 경우가 있어
  사용하지 않고, 시세/수급/종목목록 모두 네이버 증권에서 가져온다.

결과: docs/results.json 에 저장 (GitHub Pages가 이 폴더를 서빙)
"""
import os
import json
import datetime
import traceback

import pandas as pd

from indicators import calc_rsi, calc_disparity
from naver_price import get_daily_ohlcv, get_52w_high
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
W52_HIGH_TOP_N = 10
PRICE_HISTORY_DAYS = 40
INVESTOR_HISTORY_DAYS = 5
TOP_N_PER_MARKET = 150      # 시장별 스캔 대상 (시가총액 상위 N개). 조정 가능.
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")

MARKETS = {"kospi": 0, "kosdaq": 1}

# 중복(overlap) 판정에 사용할 5개 지표 (순매수는 제외)
OVERLAP_TAGS = ["rsi", "disparity", "turnover", "volume_surge", "w52_high"]


def get_label_date() -> str:
    d = datetime.date.today()
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def build_record(code: str, name: str) -> dict | None:
    """종목 하나의 RSI/이격도/거래대금/거래량급증/52주근접도/전일수급 데이터를 모아 반환."""
    try:
        price_df = get_daily_ohlcv(code, days=PRICE_HISTORY_DAYS)
        if len(price_df) < DISPARITY_MA_PERIOD + 3:
            return None

        close = price_df["close"]
        volume = price_df["volume"]

        rsi = calc_rsi(close, period=14)
        disparity = calc_disparity(close, ma_period=DISPARITY_MA_PERIOD)

        latest_close = float(close.iloc[-1])
        latest_rsi = rsi.iloc[-1]
        latest_disparity = disparity.iloc[-1]
        if pd.isna(latest_rsi) or pd.isna(latest_disparity):
            return None

        latest_volume = float(volume.iloc[-1])
        avg_volume = float(volume.tail(VOLUME_SURGE_MA_PERIOD).mean())
        volume_surge = (latest_volume / avg_volume) if avg_volume > 0 else 0.0
        turnover = latest_close * latest_volume

        week52_high = get_52w_high(code)
        w52_proximity = (latest_close / week52_high * 100) if week52_high else None

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
            "rsi": round(float(latest_rsi), 2),
            "disparity": round(float(latest_disparity), 2),
            "net_buy_1d": net_buy_1d,
            "turnover": turnover,
            "volume_surge": round(volume_surge, 2),
            "w52_proximity": round(w52_proximity, 2) if w52_proximity is not None else None,
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

    w52_candidates = [r for r in records if r["w52_proximity"] is not None]
    w52_high_list = sorted(w52_candidates, key=lambda r: r["w52_proximity"], reverse=True)[:W52_HIGH_TOP_N]

    list_by_tag = {
        "rsi": rsi_list,
        "disparity": disparity_list,
        "turnover": turnover_list,
        "volume_surge": volume_surge_list,
        "w52_high": w52_high_list,
    }

    tag_map = {}  # code -> set of matched tag names (5개 지표 중)
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
        "w52_high_top": w52_high_list,
    }


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
            "w52_high_top_n": W52_HIGH_TOP_N,
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
            f"거래량급증:{len(m['volume_surge_top'])} 52주근접:{len(m['w52_high_top'])} "
            f"중복3+:{len(m['overlap_3plus'])} 중복2+:{len(m['overlap_2plus'])}"
        )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"완료 -> {OUTPUT_PATH}")
    return result


if __name__ == "__main__":
    main()
