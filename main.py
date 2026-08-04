# -*- coding: utf-8 -*-
"""
매일 아침 실행되는 종목 스크리닝 메인 스크립트. (개별 조건별 리스트 + 중복 표시 버전)

시장(코스피/코스닥)을 나눠서, 아래 3개 리스트를 "따로" 뽑는다 (동시 만족 필요 없음):
1) RSI(14) 30 이하 종목 중 가장 과매도인 상위 10개
2) 전일 외국인+기관 순매수 합계 상위 30개 (네이버 증권 기준, 수량 단위)
3) 이격도(20일선)가 가장 낮은(=평균에서 가장 밑으로 벌어진) 하위 10개
그리고 이 3개 리스트 중 "2개 이상"에 동시에 들어간 종목을 별도로 표시한다.

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
from naver_price import get_daily_ohlcv, get_stock_name
from naver_investor import get_foreign_institution_net
from naver_universe import get_market_universe
from retry_util import retry_call

# ----------------------------------------------------------------------------
# 설정값 (필요시 조정)
# ----------------------------------------------------------------------------
RSI_THRESHOLD = 30
RSI_TOP_N = 10
NET_BUY_TOP_N = 30
DISPARITY_MA_PERIOD = 20
DISPARITY_TOP_N = 10
PRICE_HISTORY_DAYS = 40
INVESTOR_HISTORY_DAYS = 5
TOP_N_PER_MARKET = 150      # 시장별 스캔 대상 (시가총액 상위 N개). 조정 가능.
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")

MARKETS = {"kospi": 0, "kosdaq": 1}


def get_label_date() -> str:
    d = datetime.date.today()
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def build_record(code: str, name: str) -> dict | None:
    """종목 하나의 RSI/이격도/전일 수급 데이터를 모아 반환. 데이터 부족 시 None."""
    try:
        price_df = get_daily_ohlcv(code, days=PRICE_HISTORY_DAYS)
        if len(price_df) < DISPARITY_MA_PERIOD + 3:
            return None

        close = price_df["close"]
        rsi = calc_rsi(close, period=14)
        disparity = calc_disparity(close, ma_period=DISPARITY_MA_PERIOD)

        latest_rsi = rsi.iloc[-1]
        latest_disparity = disparity.iloc[-1]
        if pd.isna(latest_rsi) or pd.isna(latest_disparity):
            return None

        investor_df = get_foreign_institution_net(code, days=INVESTOR_HISTORY_DAYS)
        if investor_df.empty:
            net_buy_1d = 0
        else:
            last_row = investor_df.iloc[-1]
            net_buy_1d = int(last_row["foreign_net"] + last_row["institution_net"])

        return {
            "code": code,
            "name": name,
            "rsi": round(float(latest_rsi), 2),
            "disparity": round(float(latest_disparity), 2),
            "close": float(close.iloc[-1]),
            "net_buy_1d": net_buy_1d,
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

    # 1) RSI 30 이하 중 가장 낮은 순 상위 10개
    rsi_candidates = [r for r in records if r["rsi"] <= RSI_THRESHOLD]
    rsi_list = sorted(rsi_candidates, key=lambda r: r["rsi"])[:RSI_TOP_N]

    # 2) 전일 외국인+기관 순매수 합계 상위 30개
    net_buy_list = sorted(records, key=lambda r: r["net_buy_1d"], reverse=True)[:NET_BUY_TOP_N]

    # 3) 이격도가 가장 낮은(평균 대비 가장 많이 빠진) 하위 10개
    disparity_list = sorted(records, key=lambda r: r["disparity"])[:DISPARITY_TOP_N]

    # 2개 이상 리스트에 동시에 들어간 종목 찾기
    tag_map = {}  # code -> set of list names
    for name, lst in (("rsi", rsi_list), ("net_buy", net_buy_list), ("disparity", disparity_list)):
        for r in lst:
            tag_map.setdefault(r["code"], set()).add(name)

    record_by_code = {r["code"]: r for r in records}
    overlap_list = []
    for code, tags in tag_map.items():
        if len(tags) >= 2:
            item = dict(record_by_code[code])
            item["matched_lists"] = sorted(tags)
            overlap_list.append(item)
    overlap_list.sort(key=lambda r: -len(r["matched_lists"]))

    return {
        "scanned_count": len(records),
        "rsi_low": rsi_list,
        "net_buy_top": net_buy_list,
        "disparity_low": disparity_list,
        "overlap": overlap_list,
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
            "data_source": "naver",
        },
    }

    for market_label, sosok in MARKETS.items():
        result[market_label] = screen_market(sosok, market_label)
        m = result[market_label]
        print(
            f"[{market_label}] RSI:{len(m['rsi_low'])} "
            f"순매수:{len(m['net_buy_top'])} 이격도:{len(m['disparity_low'])} "
            f"중복:{len(m['overlap'])}"
        )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"완료 -> {OUTPUT_PATH}")
    return result


if __name__ == "__main__":
    main()
