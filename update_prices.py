# -*- coding: utf-8 -*-
"""
장중(9:00~15:30) 15분마다 실행되는 가벼운 스크립트.
전체 스크리닝(main.py)은 다시 하지 않고, 이미 결과 리스트에 있는 종목들의
"현재가/등락폭/등락률"만 빠르게 조회해서 docs/live.json에 저장한다.

브라우저에서 직접 네이버 API를 호출하면 CORS/헤더 문제로 차단되지만,
여기서는 서버(GitHub Actions)가 적절한 헤더를 갖춰서 요청하므로 문제없이 동작한다.
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


def collect_codes(results: dict) -> list:
    codes = set()
    for market in ("kospi", "kosdaq"):
        m = results.get(market) or {}
        for key in (
            "overlap_3plus", "overlap_2plus", "rsi_low", "net_buy_top",
            "disparity_low", "turnover_top", "volume_surge_top", "w52_high_top",
        ):
            for item in m.get(key) or []:
                codes.add(item["code"])
    return sorted(codes)


def fetch_prices(codes: list) -> dict:
    """네이버 실시간 폴링 API에서 현재가/등락폭/등락률을 조회. 코드가 많으면 나눠서 요청."""
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

    codes = collect_codes(results)
    print(f"현재가 조회 대상: {len(codes)}개")

    prices = fetch_prices(codes)
    print(f"조회 성공: {len(prices)}개")

    output = {
        "updated_at": datetime.datetime.now().isoformat(),
        "prices": prices,
    }
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료 -> {LIVE_PATH}")


if __name__ == "__main__":
    main()
