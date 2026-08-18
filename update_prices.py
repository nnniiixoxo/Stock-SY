# -*- coding: utf-8 -*-
"""
장중(9:00~15:30) 15분마다 실행되는 가벼운 스크립트.
전체 스크리닝(main.py)은 다시 하지 않고, 이미 결과 리스트에 있는 종목들의
"현재가/등락폭/등락률"만 빠르게 조회해서 docs/live.json에 저장한다.
"""
import os
import json
import datetime
import requests

from retry_util import retry_call

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")
LIVE_PATH = os.path.join(os.path.dirname(__file__), "docs", "live.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com",
}

POLLING_URL_TMPL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}"
INDEX_URL_TMPL = "https://polling.finance.naver.com/api/realtime/domestic/index/{code}"
INDEX_HISTORY_MAX_POINTS = 40  # 장중 5분 간격 기준 하루치 정도(약 6.5시간 -> 78포인트 여유있게 축소 보관)


def fetch_index(code: str):
    """코스피(KOSPI) / 코스닥(KOSDAQ) 지수 현재가를 조회."""
    url = INDEX_URL_TMPL.format(code=code)

    def _get():
        resp = requests.get(url, headers=HEADERS, timeout=8)
        resp.raise_for_status()
        return resp.json()

    try:
        data = retry_call(_get, retries=3, delay=2.0)
    except RuntimeError as e:
        print(f"[WARN] 지수 조회 실패({code}): {e}")
        return None

    areas = (data or {}).get("result", {}).get("areas", [])
    rows = areas[0].get("datas", []) if areas else []
    if not rows:
        return None
    row = rows[0]
    return {"value": row.get("nv"), "change": row.get("cv"), "rate": row.get("cr")}


def build_index_data(prev_indices: dict) -> dict:
    """지수 현재가 조회 + 기존 기록에 이어서 추이(history) 축적."""
    now_label = datetime.datetime.now().strftime("%H:%M")
    result = {}
    for label, code in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")):
        info = fetch_index(code)
        prev = (prev_indices or {}).get(label, {})
        history = list(prev.get("history", []))
        if info:
            history.append({"time": now_label, "value": info["value"]})
            history = history[-INDEX_HISTORY_MAX_POINTS:]
            result[label] = {
                "value": info["value"],
                "change": info["change"],
                "rate": info["rate"],
                "history": history,
            }
        else:
            # 조회 실패 시 이전 값을 그대로 유지 (화면이 비지 않도록)
            result[label] = prev
    return result


def collect_codes(results: dict) -> list:
    codes = set()
    for market in ("kospi", "kosdaq"):
        m = results.get(market) or {}
        for key in (
            "overlap_3plus", "overlap_2plus", "risky_2plus", "rsi_low", "net_buy_top",
            "disparity_low", "turnover_top", "volume_surge_top", "ma_align_top",
        ):
            for item in m.get(key) or []:
                codes.add(item["code"])
    return sorted(codes)


def fetch_prices(codes: list) -> dict:
    prices = {}
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i : i + batch_size]
        url = POLLING_URL_TMPL.format(codes=",".join(batch))

        def _get():
            resp = requests.get(url, headers=HEADERS, timeout=8)
            resp.raise_for_status()
            return resp.json()

        try:
            data = retry_call(_get, retries=3, delay=2.0)
        except RuntimeError as e:
            print(f"[WARN] 가격 조회 실패(batch {i}): {e}")
            continue

        areas = (data or {}).get("result", {}).get("areas", [])
        rows = areas[0].get("datas", []) if areas else []
        for row in rows:
            code = row.get("cd")
            if not code:
                continue
            prices[code] = {
                "price": row.get("nv"),
                "change": row.get("cv"),
                "rate": row.get("cr"),
            }
    return prices


def main():
    with open(RESULTS_PATH, encoding="utf-8") as f:
        results = json.load(f)

    prev_indices = {}
    if os.path.exists(LIVE_PATH):
        try:
            with open(LIVE_PATH, encoding="utf-8") as f:
                prev_live = json.load(f)
            prev_indices = prev_live.get("indices", {})
        except (json.JSONDecodeError, OSError):
            prev_indices = {}

    codes = collect_codes(results)
    print(f"현재가 조회 대상: {len(codes)}개")

    prices = fetch_prices(codes)
    print(f"조회 성공: {len(prices)}개")

    indices = build_index_data(prev_indices)
    print(f"지수 조회: {list(indices.keys())}")

    output = {
        "updated_at": datetime.datetime.now().isoformat(),
        "prices": prices,
        "indices": indices,
    }
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료 -> {LIVE_PATH}")


if __name__ == "__main__":
    main()
