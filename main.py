# -*- coding: utf-8 -*-
"""
매일 아침 실행되는 종목 스크리닝 메인 스크립트.

사용자가 평소 실제로 보는 6개 지표를 각각 "신호가 떴는지(참/거짓)"로 판단하고,
몇 개 지표가 동시에 신호를 내는지로 종목을 정렬한다:
1) RSI(14) 30 이하 (과매도)
2) 이동평균선 정배열 (5일선 > 20일선 > 60일선)
3) MACD 골든크로스 (MACD선이 시그널선을 최근 2일 이내 상향 돌파)
4) 슬로우 스토캐스틱 매수신호 (%K 30 이하 구간에서 %D를 최근 2일 이내 상향 돌파)
5) 외국인+기관 순매수 (최근 3거래일 연속, 둘 중 하나라도)
6) 공매도 거래량 감소 (최근 3일 평균이 그 이전 3일 평균보다 낮음, 매도 압력 완화 신호)

* KRX(pykrx)는 GitHub Actions 등 해외 서버에서 접속이 차단되는 경우가 있어
  사용하지 않고, 시세/수급/종목목록 모두 네이버 증권에서 가져온다.
* 거래정지(최근 며칠 거래량 0) 종목은 지표가 왜곡되므로 스캔에서 제외한다.

결과: docs/results.json 에 저장 (GitHub Pages가 이 폴더를 서빙)
또한, "2개 이상 중복" 종목들을 docs/history.json에 매일 누적 기록해서
나중에 사후 검증(evaluate_picks.py)에 사용한다.
"""
import os
import json
import datetime
import traceback

import pandas as pd

from indicators import calc_rsi, calc_ma, calc_macd, calc_stochastic, crossed_above
from naver_price import get_daily_ohlcv
from naver_investor import get_foreign_institution_net, has_3day_consecutive_net_buy
from naver_shortsell import get_short_sell_volume, is_short_sell_decreasing
from naver_universe import get_market_universe
from retry_util import retry_call

# ----------------------------------------------------------------------------
# 설정값 (필요시 조정)
# ----------------------------------------------------------------------------
RSI_THRESHOLD = 30
MA_SHORT = 5
MA_MID = 20
MA_LONG = 60
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
CROSS_LOOKBACK_DAYS = 2       # 골든크로스를 "최근 며칠 이내"로 볼지
STOCH_K_PERIOD = 14
STOCH_K_SLOW = 3
STOCH_D_PERIOD = 3
STOCH_OVERSOLD = 30           # 스토캐스틱 과매도 기준
NET_BUY_LOOKBACK_DAYS = 3     # 순매수 연속 판정 기간
SHORT_SELL_HISTORY_DAYS = 10
SHORT_SELL_RECENT_N = 3       # 공매도 거래량 감소 판정 시 비교할 최근/이전 구간 길이
PRICE_HISTORY_DAYS = 70       # 60일선 계산을 위해 넉넉히 확보
INVESTOR_HISTORY_DAYS = 5
TOP_N_PER_MARKET = 150
HALT_CHECK_DAYS = 3           # 최근 이 기간 중 거래량 0인 날이 있으면 거래정지로 간주해 제외
LIST_DISPLAY_TOP_N = 20       # 화면에 보여줄 각 지표별 리스트 최대 개수
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.json")
HISTORY_MAX_ENTRIES = 200    # 누적 기록 최대 보관 일수 (너무 커지지 않도록 제한)

MARKETS = {"kospi": 0, "kosdaq": 1}
OVERLAP_TAGS = ["rsi", "ma_align", "macd", "stochastic", "net_buy", "short_sell"]
KST = datetime.timezone(datetime.timedelta(hours=9))

def get_label_date() -> str:
    """
    한국시간(KST) 기준 날짜를 반환. GitHub Actions 서버는 UTC로 동작하므로,
    datetime.date.today()를 그대로 쓰면 실행 시각에 따라 하루 어긋날 수 있어 명시적으로 KST로 변환한다.
    """
    d = datetime.datetime.now(KST).date()
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def build_record(code: str, name: str) -> dict | None:
    """종목 하나의 RSI/이평선/MACD/스토캐스틱/수급 데이터를 모아 반환."""
    try:
        price_df = get_daily_ohlcv(code, days=PRICE_HISTORY_DAYS)
        min_required = max(MA_LONG, MACD_SLOW + MACD_SIGNAL) + 3
        if len(price_df) < min_required:
            return None

        close = price_df["close"]
        volume = price_df["volume"]

        # 거래정지(며칠간 거래량 0) 종목 필터링:
        # - 거래정지 중엔 가격이 안 움직여 이격도/RSI가 왜곡되고,
        # - 거래 재개 시 액면분할/감자 등으로 가격이 급변해 "폭락"처럼 잘못 잡힌다.
        # 최근 며칠 중 단 하루라도 거래량이 0이면 그 종목은 이번 스캔에서 제외한다.
        recent_volumes = volume.tail(HALT_CHECK_DAYS)
        if (recent_volumes <= 0).any():
            return None

        high = price_df["high"]
        low = price_df["low"]

        rsi = calc_rsi(close, period=14)
        ma_short = calc_ma(close, MA_SHORT)
        ma_mid = calc_ma(close, MA_MID)
        ma_long = calc_ma(close, MA_LONG)
        macd_line, macd_signal_line, macd_hist = calc_macd(
            close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL
        )
        stoch_k, stoch_d = calc_stochastic(
            high, low, close, k_period=STOCH_K_PERIOD, k_slow=STOCH_K_SLOW, d_period=STOCH_D_PERIOD
        )

        latest_date = price_df["date"].iloc[-1]
        latest_date_str = latest_date.strftime("%Y-%m-%d") if hasattr(latest_date, "strftime") else str(latest_date)

        latest_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else None
        change_pct = (
            round((latest_close - prev_close) / prev_close * 100, 2)
            if prev_close
            else None
        )

        # 진단: 등락률이 정확히 0%이거나 비정상적으로 큰 경우, 원본 데이터를 로그로 남긴다
        # (0%가 여러 종목에서 반복되면 데이터 수집 문제, 극단값은 무상증자/액면분할 등
        #  주가 조정 이벤트로 인한 착시일 가능성이 높다)
        if change_pct == 0 or (change_pct is not None and abs(change_pct) >= 25):
            tail3 = price_df[["date", "close", "volume"]].tail(3).to_string(index=False)
            print(f"[진단] {code}({name}) change_pct={change_pct}% 최근 3일 원본 데이터:\n{tail3}")

        latest_rsi = rsi.iloc[-1]
        latest_ma_short = ma_short.iloc[-1]
        latest_ma_mid = ma_mid.iloc[-1]
        latest_ma_long = ma_long.iloc[-1]
        latest_macd = macd_line.iloc[-1]
        latest_macd_signal = macd_signal_line.iloc[-1]
        latest_stoch_k = stoch_k.iloc[-1]
        latest_stoch_d = stoch_d.iloc[-1]

        if pd.isna(latest_rsi) or pd.isna(latest_ma_long) or pd.isna(latest_stoch_d):
            return None

        ma_aligned = bool(latest_ma_short > latest_ma_mid > latest_ma_long)

        macd_cross = crossed_above(macd_line, macd_signal_line, lookback=CROSS_LOOKBACK_DAYS)

        stoch_cross = crossed_above(stoch_k, stoch_d, lookback=CROSS_LOOKBACK_DAYS)
        stoch_signal = bool(stoch_cross and latest_stoch_k <= STOCH_OVERSOLD)

        investor_df = get_foreign_institution_net(code, days=INVESTOR_HISTORY_DAYS)
        if investor_df.empty:
            net_buy_1d = 0
            net_buy_signal = False
        else:
            last_row = investor_df.iloc[-1]
            net_buy_1d = int(last_row["foreign_net"] + last_row["institution_net"])
            net_buy_signal = has_3day_consecutive_net_buy(investor_df)

        short_sell_df = get_short_sell_volume(code, days=SHORT_SELL_HISTORY_DAYS)
        if short_sell_df.empty:
            short_sell_latest = 0
            short_sell_signal = False
        else:
            short_sell_latest = int(short_sell_df["short_volume"].iloc[-1])
            short_sell_signal = is_short_sell_decreasing(short_sell_df, recent_n=SHORT_SELL_RECENT_N)

        rsi_signal = bool(latest_rsi <= RSI_THRESHOLD)

        return {
            "code": code,
            "name": name,
            "close": latest_close,
            "change_pct": change_pct,
            "data_date": latest_date_str,
            "rsi": round(float(latest_rsi), 2),
            "rsi_signal": rsi_signal,
            "ma_aligned": ma_aligned,
            "ma_signal": ma_aligned,
            "macd": round(float(latest_macd), 2),
            "macd_signal_line": round(float(latest_macd_signal), 2),
            "macd_signal": bool(macd_cross),
            "stoch_k": round(float(latest_stoch_k), 2),
            "stoch_d": round(float(latest_stoch_d), 2),
            "stochastic_signal": stoch_signal,
            "net_buy_1d": net_buy_1d,
            "net_buy_signal": bool(net_buy_signal),
            "short_sell_latest": short_sell_latest,
            "short_sell_signal": bool(short_sell_signal),
        }
    except Exception:
        print(f"[WARN] {code} 처리 중 오류:\n{traceback.format_exc()}")
        return None


def rank_and_tag_records(records: list) -> dict:
    """
    종목 record 리스트(하루치, 한 시장)를 받아서 5개 신호별 리스트 + 중복 판정을 계산.
    main.py(실시간 스크리닝)와 backtest.py(과거 검증)가 동일 로직을 쓰도록 분리.
    """
    # 실제로 수집된 데이터가 어느 거래일 기준인지 (가장 흔한 날짜 = 대부분 종목의 최신 종가일)
    date_counts: dict = {}
    for r in records:
        d = r.get("data_date")
        if d:
            date_counts[d] = date_counts.get(d, 0) + 1
    actual_data_date = max(date_counts, key=date_counts.get) if date_counts else None

    rsi_list = sorted(
        [r for r in records if r["rsi_signal"]], key=lambda r: r["rsi"]
    )[:LIST_DISPLAY_TOP_N]

    ma_list = [r for r in records if r["ma_signal"]][:LIST_DISPLAY_TOP_N]

    macd_list = [r for r in records if r["macd_signal"]][:LIST_DISPLAY_TOP_N]

    stochastic_list = sorted(
        [r for r in records if r["stochastic_signal"]], key=lambda r: r["stoch_k"]
    )[:LIST_DISPLAY_TOP_N]

    net_buy_list = sorted(
        [r for r in records if r["net_buy_signal"]], key=lambda r: r["net_buy_1d"], reverse=True
    )[:LIST_DISPLAY_TOP_N]

    short_sell_list = sorted(
        [r for r in records if r["short_sell_signal"]], key=lambda r: r["short_sell_latest"]
    )[:LIST_DISPLAY_TOP_N]

    list_by_tag = {
        "rsi": rsi_list,
        "ma_align": ma_list,
        "macd": macd_list,
        "stochastic": stochastic_list,
        "net_buy": net_buy_list,
        "short_sell": short_sell_list,
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

    overlap_6 = build_overlap(6)
    overlap_5plus = build_overlap(5)
    overlap_4plus = build_overlap(4)
    overlap_3plus = build_overlap(3)
    overlap_2plus = build_overlap(2)

    return {
        "scanned_count": len(records),
        "actual_data_date": actual_data_date,
        "overlap_6": overlap_6,
        "overlap_5plus": overlap_5plus,
        "overlap_4plus": overlap_4plus,
        "overlap_3plus": overlap_3plus,
        "overlap_2plus": overlap_2plus,
        "rsi_low": rsi_list,
        "ma_align_top": ma_list,
        "macd_top": macd_list,
        "stochastic_top": stochastic_list,
        "net_buy_top": net_buy_list,
        "short_sell_top": short_sell_list,
    }


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

    return rank_and_tag_records(records)


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
        "generated_at": datetime.datetime.now(KST).isoformat(),
        "conditions": {
            "rsi_threshold": RSI_THRESHOLD,
            "ma_periods": [MA_SHORT, MA_MID, MA_LONG],
            "macd_periods": [MACD_FAST, MACD_SLOW, MACD_SIGNAL],
            "cross_lookback_days": CROSS_LOOKBACK_DAYS,
            "stochastic_periods": [STOCH_K_PERIOD, STOCH_K_SLOW, STOCH_D_PERIOD],
            "stochastic_oversold": STOCH_OVERSOLD,
            "net_buy_lookback_days": NET_BUY_LOOKBACK_DAYS,
            "short_sell_recent_n": SHORT_SELL_RECENT_N,
            "overlap_tags": OVERLAP_TAGS,
            "data_source": "naver",
        },
    }

    for market_label, sosok in MARKETS.items():
        result[market_label] = screen_market(sosok, market_label)
        m = result[market_label]
        print(
            f"[{market_label}] RSI:{len(m['rsi_low'])} 이평선:{len(m['ma_align_top'])} "
            f"MACD:{len(m['macd_top'])} 스토캐스틱:{len(m['stochastic_top'])} "
            f"수급:{len(m['net_buy_top'])} 공매도감소:{len(m['short_sell_top'])} "
            f"중복6:{len(m['overlap_6'])} 중복5+:{len(m['overlap_5plus'])} "
            f"중복4+:{len(m['overlap_4plus'])} 중복3+:{len(m['overlap_3plus'])} 중복2+:{len(m['overlap_2plus'])}"
        )

    actual_dates = {result[m].get("actual_data_date") for m in MARKETS if result[m].get("actual_data_date")}
    actual_data_date = sorted(actual_dates)[-1] if actual_dates else None
    result["actual_data_date"] = actual_data_date
    if actual_data_date and actual_data_date.replace("-", "") != base_date:
        print(
            f"[WARN] 라벨은 '{base_date}'인데 실제 최신 데이터는 '{actual_data_date}' 기준임 "
            f"(장 시작 전 등, 아직 당일 종가가 없을 때 실행된 경우 정상적으로 발생할 수 있음)"
        )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"완료 -> {OUTPUT_PATH}")

    append_history(base_date, result)

    return result


if __name__ == "__main__":
    main()
