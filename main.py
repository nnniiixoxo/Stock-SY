# -*- coding: utf-8 -*-
"""
매일 아침 실행되는 종목 스크리닝 메인 스크립트 (점수제).

아래 8개 조건 각각에 점수를 매기고, 합산 점수로 종목을 등급화한다.
(★ 원래 조건표에는 VWAP/5분봉/당일상승 등 4개가 더 있었지만, 이 4개는 장이
  열려야만 계산 가능한 "장중 실시간" 지표라 장 시작 전(07:45) 실행 구조에서는
  값 자체가 존재하지 않는다. 그래서 이번 구현에서는 제외했다. 나중에 장중
  실시간 모니터링을 별도로 만들면 그때 합산할 수 있다.)

조건과 배점 (합계 12점 만점):
- MA5 > MA20 > MA60 (정배열)                          +2
- MA20 상승 (오늘 20일선 > 어제 20일선)                +1
- MA60 상승 (오늘 60일선 > 어제 60일선)                +1
- RSI 35~50 구간                                       +1
- RSI 상승 전환 (어제 30 이하 → 오늘 30 상향 돌파)      +2
- 이격도 저점 → 상승 전환 (어제 이격도가 그저께보다 낮고, 오늘 다시 상승 + 어제 값이 100 미만) +2
- RVOL(상대거래량) >= 2 (당일 거래량 / 20일 평균 >= 2배)  +2
- 거래대금 증가 (당일 거래대금 > 20일 평균 거래대금)     +1

등급: 8점 이상 관심종목 / 10점 이상 매수 후보 / 12점(전체 만족) 강한 매수 후보

* KRX(pykrx)는 GitHub Actions 등 해외 서버에서 접속이 차단되는 경우가 있어
  사용하지 않고, 시세/종목목록 모두 네이버 증권에서 가져온다.
* 거래정지(최근 며칠 거래량 0) 종목은 지표가 왜곡되므로 스캔에서 제외한다.
* 코스피/코스닥 시가총액 상위 50%를 병렬로 스캔한다.

결과: docs/results.json 에 저장 (GitHub Pages가 이 폴더를 서빙)
또한, 관심종목 이상(8점+) 픽들을 docs/history.json에 매일 누적 기록해서
나중에 사후 검증(evaluate_picks.py)에 사용한다.
"""
import os
import json
import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from indicators import calc_rsi, calc_disparity, calc_ma
from naver_price import get_daily_ohlcv
from naver_universe import get_market_universe
from retry_util import retry_call

# ----------------------------------------------------------------------------
# 설정값 (필요시 조정)
# ----------------------------------------------------------------------------
DISPARITY_MA_PERIOD = 20
VOLUME_SURGE_MA_PERIOD = 20      # RVOL, 거래대금 평균 계산에 공용으로 사용
MA_SHORT = 5
MA_MID = 20
MA_LONG = 60
RSI_UPCROSS_LEVEL = 30           # RSI가 이 값을 상향 돌파하는 순간을 "상승 전환"으로 판단
RSI_ZONE_LOW = 35                # RSI 35~50 구간 하한
RSI_ZONE_HIGH = 50               # RSI 35~50 구간 상한
RVOL_THRESHOLD = 2.0             # RVOL(상대거래량) 기준치
PRICE_HISTORY_DAYS = 70          # 60일선 계산을 위해 넉넉히 확보
TOP_N_PER_MARKET = 3000          # 시장 전체 목록을 가져오기 위한 값 (실제 상장 종목 수보다 크게 설정)
SCAN_PERCENTAGE = 0.5            # 그중 시가총액 상위 몇 %만 실제로 스캔할지 (0.5 = 상위 50%)
HALT_CHECK_DAYS = 3              # 최근 이 기간 중 거래량 0인 날이 있으면 거래정지로 간주해 제외
SCAN_WORKERS = 8                 # 시세 조회 동시 요청 수 (너무 높이면 차단 위험)
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.json")
HISTORY_MAX_ENTRIES = 200        # 누적 기록 최대 보관 일수 (너무 커지지 않도록 제한)

MARKETS = {"kospi": 0, "kosdaq": 1}

# 등급 기준 점수
TIER_STRONG_BUY = 12   # 강한 매수 후보 (사실상 8개 조건 전부 만족해야 도달)
TIER_BUY_CANDIDATE = 10  # 매수 후보
TIER_WATCH = 8          # 관심종목

# 각 조건의 배점 (참고/문서용 -- 실제 배점은 build_price_record 안에 그대로 반영되어 있음)
SCORE_RULES = [
    ("ma_aligned", "정배열 (MA5>MA20>MA60)", 2),
    ("ma_mid_rising", "MA20 상승", 1),
    ("ma_long_rising", "MA60 상승", 1),
    ("rsi_zone", "RSI 35~50", 1),
    ("rsi_upcross", "RSI 상승 전환", 2),
    ("disparity_bottom_turn", "이격도 저점->상승", 2),
    ("rvol_high", "RVOL>=2", 2),
    ("turnover_increase", "거래대금 증가", 1),
]

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


def build_price_record(code: str, name: str) -> dict | None:
    """종목 하나의 지표를 계산하고, 8개 조건 점수를 합산해서 반환."""
    try:
        price_df = get_daily_ohlcv(code, days=PRICE_HISTORY_DAYS)
        min_required = max(DISPARITY_MA_PERIOD, VOLUME_SURGE_MA_PERIOD, MA_LONG) + 3
        if len(price_df) < min_required:
            return None

        close = price_df["close"]
        volume = price_df["volume"]

        recent_volumes = volume.tail(HALT_CHECK_DAYS)
        if (recent_volumes <= 0).any():
            return None

        rsi = calc_rsi(close, period=14)
        disparity = calc_disparity(close, ma_period=DISPARITY_MA_PERIOD)
        ma_short = calc_ma(close, MA_SHORT)
        ma_mid = calc_ma(close, MA_MID)
        ma_long = calc_ma(close, MA_LONG)

        latest_date = price_df["date"].iloc[-1]
        latest_date_str = latest_date.strftime("%Y-%m-%d") if hasattr(latest_date, "strftime") else str(latest_date)

        latest_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else None
        change_pct = (
            round((latest_close - prev_close) / prev_close * 100, 2)
            if prev_close
            else None
        )

        if change_pct == 0 or (change_pct is not None and abs(change_pct) >= 25):
            tail3 = price_df[["date", "close", "volume"]].tail(3).to_string(index=False)
            print(f"[진단] {code}({name}) change_pct={change_pct}% 최근 3일 원본 데이터:\n{tail3}")

        latest_rsi = rsi.iloc[-1]
        latest_disparity = disparity.iloc[-1]
        latest_ma_short = ma_short.iloc[-1]
        latest_ma_mid = ma_mid.iloc[-1]
        latest_ma_long = ma_long.iloc[-1]

        if pd.isna(latest_rsi) or pd.isna(latest_disparity) or pd.isna(latest_ma_long):
            return None
        if len(rsi) < 2 or len(disparity) < 3:
            return None

        latest_volume = float(volume.iloc[-1])
        avg_volume = float(volume.tail(VOLUME_SURGE_MA_PERIOD).mean())
        rvol = (latest_volume / avg_volume) if avg_volume > 0 else 0.0

        turnover_series = close * volume
        latest_turnover = float(turnover_series.iloc[-1])
        avg_turnover = float(turnover_series.tail(VOLUME_SURGE_MA_PERIOD).mean())

        ma_aligned = bool(latest_ma_short > latest_ma_mid > latest_ma_long)
        ma_mid_rising = bool(ma_mid.iloc[-1] > ma_mid.iloc[-2])
        ma_long_rising = bool(ma_long.iloc[-1] > ma_long.iloc[-2])
        rsi_zone = bool(RSI_ZONE_LOW <= latest_rsi <= RSI_ZONE_HIGH)
        rsi_upcross = bool(rsi.iloc[-2] <= RSI_UPCROSS_LEVEL < rsi.iloc[-1])

        recent_disparity = disparity.tail(3)
        prev_disparity = recent_disparity.iloc[-2]
        prev2_disparity = recent_disparity.iloc[-3] if len(recent_disparity) >= 3 else None
        # "저점 형성 후 상승 전환": 어제가 그저께보다 낮거나 같았고(국소 저점), 오늘 다시 올라옴.
        # 추가로 어제 이격도가 100 미만(=주가가 20일 평균 아래)이어야 "진짜 눌림목"으로 인정한다.
        disparity_bottom_turn = bool(
            prev2_disparity is not None
            and prev_disparity <= prev2_disparity
            and latest_disparity > prev_disparity
            and prev_disparity < 100
        )

        rvol_high = bool(rvol >= RVOL_THRESHOLD)
        turnover_increase = bool(latest_turnover > avg_turnover)

        conditions = {
            "ma_aligned": ma_aligned,
            "ma_mid_rising": ma_mid_rising,
            "ma_long_rising": ma_long_rising,
            "rsi_zone": rsi_zone,
            "rsi_upcross": rsi_upcross,
            "disparity_bottom_turn": disparity_bottom_turn,
            "rvol_high": rvol_high,
            "turnover_increase": turnover_increase,
        }

        score = 0
        matched = []
        for key, label, points in SCORE_RULES:
            if conditions[key]:
                score += points
                matched.append({"label": label, "points": points})

        if score >= TIER_STRONG_BUY:
            tier = "strong_buy"
        elif score >= TIER_BUY_CANDIDATE:
            tier = "buy_candidate"
        elif score >= TIER_WATCH:
            tier = "watch"
        else:
            tier = None

        return {
            "code": code,
            "name": name,
            "close": latest_close,
            "change_pct": change_pct,
            "data_date": latest_date_str,
            "rsi": round(float(latest_rsi), 2),
            "disparity": round(float(latest_disparity), 2),
            "rvol": round(rvol, 2),
            "turnover": latest_turnover,
            "score": score,
            "tier": tier,
            "matched": matched,
            **conditions,
        }
    except Exception:
        print(f"[WARN] {code} 처리 중 오류:\n{traceback.format_exc()}")
        return None


def rank_by_score(records: list) -> dict:
    """
    종목 record 리스트(하루치, 한 시장)를 점수 기준으로 등급화.
    main.py(실시간 스크리닝)와 backtest.py(과거 검증)가 동일 로직을 쓰도록 분리.
    """
    date_counts: dict = {}
    for r in records:
        d = r.get("data_date")
        if d:
            date_counts[d] = date_counts.get(d, 0) + 1
    actual_data_date = max(date_counts, key=date_counts.get) if date_counts else None

    scored = sorted(records, key=lambda r: -r["score"])
    strong_buy = [r for r in scored if r["tier"] == "strong_buy"]
    buy_candidate = [r for r in scored if r["tier"] == "buy_candidate"]
    watch = [r for r in scored if r["tier"] == "watch"]

    return {
        "scanned_count": len(records),
        "actual_data_date": actual_data_date,
        "strong_buy": strong_buy,
        "buy_candidate": buy_candidate,
        "watch": watch,
    }


def screen_market(sosok: int, market_label: str) -> dict:
    full_list = retry_call(get_market_universe, sosok, TOP_N_PER_MARKET, retries=3, delay=3.0)
    cutoff = max(1, int(len(full_list) * SCAN_PERCENTAGE))
    stock_list = full_list[:cutoff]
    print(f"[{market_label}] 전체 상장 종목: {len(full_list)}개 -> 시가총액 상위 {SCAN_PERCENTAGE*100:.0f}%인 {len(stock_list)}개 스캔")

    records = []
    done = 0
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {
            executor.submit(build_price_record, item["code"], item["name"]): item
            for item in stock_list
        }
        for future in as_completed(futures):
            rec = future.result()
            if rec:
                records.append(rec)
            done += 1
            if done % 200 == 0:
                print(f"[{market_label}] 시세 조회 진행: {done}/{len(stock_list)}")

    print(f"[{market_label}] 유효 데이터 확보: {len(records)}개")
    return rank_by_score(records)


def append_history(base_date: str, result: dict):
    """
    관심종목(8점) 이상 픽들을 매일 docs/history.json에 누적 기록.
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
    entries = [e for e in entries if e.get("date") != base_date]

    def slim(items):
        return [
            {
                "code": it["code"],
                "name": it["name"],
                "close": it["close"],
                "change_pct": it.get("change_pct"),
                "score": it.get("score"),
                "tier": it.get("tier"),
                "matched": it.get("matched", []),
            }
            for it in items
        ]

    entries.append(
        {
            "date": base_date,
            "kospi": slim(
                result["kospi"]["strong_buy"] + result["kospi"]["buy_candidate"] + result["kospi"]["watch"]
            ),
            "kosdaq": slim(
                result["kosdaq"]["strong_buy"] + result["kosdaq"]["buy_candidate"] + result["kosdaq"]["watch"]
            ),
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
            "score_rules": [{"key": k, "label": label, "points": pts} for k, label, pts in SCORE_RULES],
            "tier_strong_buy": TIER_STRONG_BUY,
            "tier_buy_candidate": TIER_BUY_CANDIDATE,
            "tier_watch": TIER_WATCH,
            "ma_periods": [MA_SHORT, MA_MID, MA_LONG],
            "disparity_ma_period": DISPARITY_MA_PERIOD,
            "rvol_threshold": RVOL_THRESHOLD,
            "data_source": "naver",
        },
    }

    for market_label, sosok in MARKETS.items():
        result[market_label] = screen_market(sosok, market_label)
        m = result[market_label]
        print(
            f"[{market_label}] 강한매수후보(12점):{len(m['strong_buy'])} "
            f"매수후보(10점+):{len(m['buy_candidate'])} 관심종목(8점+):{len(m['watch'])}"
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
