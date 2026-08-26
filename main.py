# -*- coding: utf-8 -*-
"""
매일 아침 실행되는 종목 스크리닝 메인 스크립트.

아래 6개 지표를 각각 "신호가 떴는지(참/거짓)" 또는 "상위권인지"로 판단하고,
몇 개 지표가 동시에 신호를 내는지로 종목을 정렬한다:
1) RSI(14) 30 이하 (과매도)
2) 이격도(20일선)가 가장 낮은(=주가가 평균선에서 가장 많이 아래로 벌어진) 하위권
3) 거래대금(종가×거래량) 상위권
4) 거래량 급증배수(당일거래량 / 20일평균거래량) 상위권
5) 이동평균선 정배열 (5일선 > 20일선 > 60일선)
6) 공매도 거래량 감소 (최근 3일 평균이 그 이전 3일 평균보다 낮음, 매도 압력 완화 신호)

* 이격도/거래대금/거래량급증은 "방향(상승/하락)을 구분하지 못하는" 지표라, 급락 당일에도
  걸릴 수 있다. 이 3개 지표끼리만 겹치는 조합은 일반 중복 리스트가 아닌 별도의
  "하락 주의" 리스트로 분리해서 표시한다.
* KRX(pykrx)는 GitHub Actions 등 해외 서버에서 접속이 차단되는 경우가 있어
  사용하지 않고, 시세/공매도/종목목록 모두 네이버 증권에서 가져온다.
* 거래정지(최근 며칠 거래량 0) 종목은 지표가 왜곡되므로 스캔에서 제외한다.
* 코스피/코스닥 전 종목을 스캔하되, 시세 조회는 병렬로 처리하고 공매도 조회는
  1차 지표(RSI/이격도/거래대금/거래량급증/정배열)에서 이미 상위권으로 뽑힌 후보
  종목에 대해서만 수행해서 전체 실행 시간을 줄인다.

결과: docs/results.json 에 저장 (GitHub Pages가 이 폴더를 서빙)
또한, "2개 이상 중복" 종목들을 docs/history.json에 매일 누적 기록해서
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
from naver_shortsell import get_short_sell_volume, is_short_sell_decreasing
from naver_universe import get_market_universe
from retry_util import retry_call

# ----------------------------------------------------------------------------
# 설정값 (필요시 조정)
# ----------------------------------------------------------------------------
RSI_THRESHOLD = 30
RSI_TOP_N = 10
DISPARITY_MA_PERIOD = 20
DISPARITY_TOP_N = 10
TURNOVER_TOP_N = 10
VOLUME_SURGE_TOP_N = 10
VOLUME_SURGE_MA_PERIOD = 20
MA_SHORT = 5
MA_MID = 20
MA_LONG = 60
MA_ALIGN_TOP_N = 10
SHORT_SELL_HISTORY_DAYS = 10
SHORT_SELL_RECENT_N = 3       # 공매도 거래량 감소 판정 시 비교할 최근/이전 구간 길이
SHORT_SELL_TOP_N = 10
PRICE_HISTORY_DAYS = 70       # 60일선 계산을 위해 넉넉히 확보
TOP_N_PER_MARKET = 3000       # 시장 전체 목록을 가져오기 위한 값 (실제 상장 종목 수보다 크게 설정)
SCAN_PERCENTAGE = 0.5         # 그중 시가총액 상위 몇 %만 실제로 스캔할지 (0.5 = 상위 50%)
HALT_CHECK_DAYS = 3           # 최근 이 기간 중 거래량 0인 날이 있으면 거래정지로 간주해 제외
LIST_DISPLAY_TOP_N = 20       # 화면에 보여줄 각 지표별 리스트 최대 개수
SCAN_WORKERS = 8              # 시세/공매도 조회 동시 요청 수 (너무 높이면 차단 위험)
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.json")
HISTORY_MAX_ENTRIES = 200    # 누적 기록 최대 보관 일수 (너무 커지지 않도록 제한)

MARKETS = {"kospi": 0, "kosdaq": 1}
OVERLAP_TAGS = ["rsi", "disparity", "turnover", "volume_surge", "ma_align", "short_sell"]

# 방향(상승/하락)을 구분 못 하는 지표끼리만 겹치는 조합은 급락 당일에도 걸릴 수 있어
# 별도의 "하락 주의" 리스트로 분리한다.
RISKY_COMBOS = [
    {"disparity", "volume_surge"},
    {"turnover", "volume_surge"},
    {"disparity", "rsi"},
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
    """종목 하나의 RSI/이격도/거래대금/거래량급증/정배열 데이터를 모아 반환 (공매도 제외)."""
    try:
        price_df = get_daily_ohlcv(code, days=PRICE_HISTORY_DAYS)
        min_required = max(DISPARITY_MA_PERIOD, VOLUME_SURGE_MA_PERIOD, MA_LONG) + 3
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

        # 진단: 등락률이 정확히 0%이거나 비정상적으로 큰 경우, 원본 데이터를 로그로 남긴다
        # (0%가 여러 종목에서 반복되면 데이터 수집 문제, 극단값은 무상증자/액면분할 등
        #  주가 조정 이벤트로 인한 착시일 가능성이 높다)
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

        return {
            "code": code,
            "name": name,
            "close": latest_close,
            "change_pct": change_pct,
            "data_date": latest_date_str,
            "rsi": round(float(latest_rsi), 2),
            "rsi_signal": bool(latest_rsi <= RSI_THRESHOLD),
            "disparity": round(float(latest_disparity), 2),
            "turnover": turnover,
            "volume_surge": round(volume_surge, 2),
            "ma_aligned": ma_aligned,
            "ma_signal": ma_aligned,
            "ma_strength": ma_strength,
            "short_sell_latest": 0,       # 1차 스캔 시점엔 아직 미조회 (후보만 나중에 채움)
            "short_sell_signal": False,
        }
    except Exception:
        print(f"[WARN] {code} 처리 중 오류:\n{traceback.format_exc()}")
        return None


def attach_short_sell(record: dict) -> dict:
    """이미 만들어진 record에 공매도 거래량 감소 여부를 채워 넣는다 (후보 종목에 대해서만 호출)."""
    try:
        short_sell_df = get_short_sell_volume(record["code"], days=SHORT_SELL_HISTORY_DAYS)
        if not short_sell_df.empty:
            record["short_sell_latest"] = int(short_sell_df["short_volume"].iloc[-1])
            record["short_sell_signal"] = bool(
                is_short_sell_decreasing(short_sell_df, recent_n=SHORT_SELL_RECENT_N)
            )
    except Exception:
        print(f"[WARN] {record['code']} 공매도 조회 중 오류:\n{traceback.format_exc()}")
    return record


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

    rsi_candidates = [r for r in records if r["rsi_signal"]]
    rsi_list = sorted(rsi_candidates, key=lambda r: r["rsi"])[:RSI_TOP_N]

    disparity_list = sorted(records, key=lambda r: r["disparity"])[:DISPARITY_TOP_N]

    turnover_list = sorted(records, key=lambda r: r["turnover"], reverse=True)[:TURNOVER_TOP_N]

    volume_surge_list = sorted(records, key=lambda r: r["volume_surge"], reverse=True)[:VOLUME_SURGE_TOP_N]

    ma_align_candidates = [r for r in records if r["ma_aligned"]]
    ma_align_list = sorted(
        ma_align_candidates, key=lambda r: (r["ma_strength"] or 0), reverse=True
    )[:MA_ALIGN_TOP_N]

    short_sell_list = sorted(
        [r for r in records if r["short_sell_signal"]], key=lambda r: r["short_sell_latest"]
    )[:SHORT_SELL_TOP_N]

    list_by_tag = {
        "rsi": rsi_list,
        "disparity": disparity_list,
        "turnover": turnover_list,
        "volume_surge": volume_surge_list,
        "ma_align": ma_align_list,
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

    def is_risky_combo(matched_lists):
        return set(matched_lists) in RISKY_COMBOS

    overlap_6 = build_overlap(6)
    overlap_5plus = build_overlap(5)
    overlap_4plus = build_overlap(4)
    overlap_3plus = build_overlap(3)
    overlap_2plus_all = build_overlap(2)

    overlap_2plus = [r for r in overlap_2plus_all if not is_risky_combo(r["matched_lists"])]
    risky_2plus = [r for r in overlap_2plus_all if is_risky_combo(r["matched_lists"])]

    return {
        "scanned_count": len(records),
        "actual_data_date": actual_data_date,
        "overlap_6": overlap_6,
        "overlap_5plus": overlap_5plus,
        "overlap_4plus": overlap_4plus,
        "overlap_3plus": overlap_3plus,
        "overlap_2plus": overlap_2plus,
        "risky_2plus": risky_2plus,
        "rsi_low": rsi_list,
        "disparity_low": disparity_list,
        "turnover_top": turnover_list,
        "volume_surge_top": volume_surge_list,
        "ma_align_top": ma_align_list,
        "short_sell_top": short_sell_list,
    }


def screen_market(sosok: int, market_label: str) -> dict:
    full_list = retry_call(get_market_universe, sosok, TOP_N_PER_MARKET, retries=3, delay=3.0)
    cutoff = max(1, int(len(full_list) * SCAN_PERCENTAGE))
    stock_list = full_list[:cutoff]  # get_market_universe가 이미 시가총액 내림차순으로 정렬해서 반환함
    print(f"[{market_label}] 전체 상장 종목: {len(full_list)}개 → 시가총액 상위 {SCAN_PERCENTAGE*100:.0f}%인 {len(stock_list)}개 스캔")

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

    # 1차(가격 지표만) 판정으로 후보를 추려서, 공매도는 후보 종목에 대해서만 조회한다
    # (전 종목 대상으로 공매도까지 조회하면 요청 수가 너무 많아지므로)
    preliminary = rank_and_tag_records(records)
    candidate_codes = set()
    for key in ("rsi_low", "disparity_low", "turnover_top", "volume_surge_top", "ma_align_top"):
        candidate_codes.update(r["code"] for r in preliminary[key])
    print(f"[{market_label}] 공매도 조회 대상(후보): {len(candidate_codes)}개")

    record_by_code = {r["code"]: r for r in records}
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = [executor.submit(attach_short_sell, record_by_code[c]) for c in candidate_codes]
        for future in as_completed(futures):
            future.result()

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
            "rsi_top_n": RSI_TOP_N,
            "disparity_ma_period": DISPARITY_MA_PERIOD,
            "disparity_top_n": DISPARITY_TOP_N,
            "turnover_top_n": TURNOVER_TOP_N,
            "volume_surge_top_n": VOLUME_SURGE_TOP_N,
            "ma_periods": [MA_SHORT, MA_MID, MA_LONG],
            "ma_align_top_n": MA_ALIGN_TOP_N,
            "short_sell_recent_n": SHORT_SELL_RECENT_N,
            "overlap_tags": OVERLAP_TAGS,
            "data_source": "naver",
        },
    }

    for market_label, sosok in MARKETS.items():
        result[market_label] = screen_market(sosok, market_label)
        m = result[market_label]
        print(
            f"[{market_label}] RSI:{len(m['rsi_low'])} 이격도:{len(m['disparity_low'])} "
            f"거래대금:{len(m['turnover_top'])} 거래량급증:{len(m['volume_surge_top'])} "
            f"정배열:{len(m['ma_align_top'])} 공매도감소:{len(m['short_sell_top'])} "
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
