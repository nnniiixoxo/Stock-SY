# -*- coding: utf-8 -*-
"""
코스피/코스닥 종목 목록(코드/이름/시가총액)을 가져온다.

2026-09 시점 상태:
네이버 증권의 예전 HTML 페이지(finance.naver.com/sise/sise_market_sum.naver)가
Next.js 기반 클라이언트 렌더링(CSR)으로 전면 개편되면서, requests로 받아온 원본
HTML에는 표 데이터가 아예 들어있지 않게 됐다(자바스크립트가 실행된 후에야 채워짐).
그래서 새 정식 API인 stock.naver.com의 JSON 엔드포인트로 전환했다:
  GET https://stock.naver.com/api/domestic/market/stock/default
      ?tradeType=KRX&marketType={KOSPI|KOSDAQ}&orderType=marketSum
      &startIdx={n}&pageSize={n}
(orderType=marketSum 이 "시가총액순" 정렬을 의미함)

이 API는 비공식/미문서화 상태라 응답 JSON의 정확한 필드명을 100% 확신할 수 없어서,
여러 후보 키를 순서대로 시도하고, 전부 실패하면 실제 응답 구조를 로그로 남겨서
다음에 바로 고칠 수 있게 만들었다. 혹시 이 API마저 막히면 예전 HTML 파싱 방식으로도
한 번 더 시도한다(도움이 안 될 가능성이 높지만 비용이 없어 남겨둠).
"""
import re
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://stock.naver.com/",
    "Accept": "application/json",
}

MARKET_STOCK_API = "https://stock.naver.com/api/domestic/market/stock/default"
MARKET_TYPE_MAP = {0: "KOSPI", 1: "KOSDAQ"}

# 응답 JSON에서 종목 리스트가 들어있을 만한 후보 키 (최상위 dict일 경우)
LIST_KEY_CANDIDATES = ("stocks", "items", "list", "content", "data", "result")
# 종목 하나(dict)에서 코드/이름/시가총액을 찾을 만한 후보 키
CODE_KEY_CANDIDATES = ("itemCode", "code", "stockCode", "symbol")
NAME_KEY_CANDIDATES = ("stockName", "itemName", "name", "korName")
CAP_KEY_CANDIDATES = ("marketSum", "marketCap", "marketValue", "marketSumFormatted")


def _to_number(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _extract_items(data):
    """응답 JSON에서 종목 리스트(list[dict])를 찾아 반환. 못 찾으면 None."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in LIST_KEY_CANDIDATES:
            val = data.get(key)
            if isinstance(val, list):
                return val
    return None


def _row_from_item(item: dict):
    code = next((item[k] for k in CODE_KEY_CANDIDATES if item.get(k)), None)
    name = next((item[k] for k in NAME_KEY_CANDIDATES if item.get(k)), None)
    cap_raw = next((item[k] for k in CAP_KEY_CANDIDATES if item.get(k) is not None), None)
    if not code or not name:
        return None
    # 코드가 "A005930" 형태로 올 수도 있어 6자리 숫자만 추출
    m = re.search(r"(\d{6})", str(code))
    if not m:
        return None
    return {"code": m.group(1), "name": name, "market_cap": _to_number(cap_raw)}


def _fetch_via_api(market_type: str, start_idx: int, page_size: int = 100):
    params = {
        "tradeType": "KRX",
        "marketType": market_type,
        "orderType": "marketSum",
        "startIdx": start_idx,
        "pageSize": page_size,
    }
    resp = requests.get(MARKET_STOCK_API, params=params, headers=HEADERS, timeout=8)
    if start_idx == 0:
        print(f"[진단] 종목 목록 API(marketType={market_type}) 응답 상태코드: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()

    items = _extract_items(data)
    if items is None:
        if start_idx == 0:
            top_keys = list(data.keys()) if isinstance(data, dict) else f"(list, 길이 {len(data)})"
            print(
                f"[WARN] 종목 목록 API(marketType={market_type}) 응답에서 리스트를 못 찾음. "
                f"최상위 키: {top_keys}, 응답 앞부분: {str(data)[:500]}"
            )
        return []

    rows = []
    dropped = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        row = _row_from_item(it)
        if row:
            rows.append(row)
        else:
            dropped += 1
    if start_idx == 0 and rows:
        print(f"[진단] 종목 목록 API(marketType={market_type}) 첫 페이지 파싱 성공: {len(rows)}개 (누락 {dropped}개)")
    elif start_idx == 0 and not rows and items:
        sample = items[0] if items else {}
        print(
            f"[WARN] 종목 목록 API(marketType={market_type}) 항목은 있지만 필드 매칭 실패. "
            f"첫 항목 키: {list(sample.keys()) if isinstance(sample, dict) else sample}"
        )
    return rows


def _fetch_via_legacy_html(sosok: int, page: int):
    """예전 HTML 페이지 방식 (2026-09 기준 더 이상 동작하지 않을 가능성이 높지만 최후 수단으로 유지)."""
    resp = requests.get(
        "https://finance.naver.com/sise/sise_market_sum.naver",
        params={"sosok": sosok, "page": page},
        headers={**HEADERS, "Accept": "text/html"},
        timeout=5,
    )
    resp.raise_for_status()
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.select_one("table.type_2")
    if table is None:
        for t in soup.find_all("table"):
            header_text = t.find("thead")
            header_text = header_text.get_text() if header_text else t.get_text()[:300]
            if "종목명" in header_text and "시가총액" in header_text:
                table = t
                break
    if table is None:
        return []

    header_cols = [th.get_text(strip=True) for th in table.select("thead th")] or \
                  [th.get_text(strip=True) for th in table.select("th")]
    cap_col_idx = next((i for i, h in enumerate(header_cols) if "시가총액" in h), None)

    rows = []
    for tr in (table.select("tbody tr") or table.select("tr")):
        link = tr.select_one("a.tltle")
        if link is None:
            continue
        href = link.get("href", "")
        m = re.search(r"code=(\d{6})", href)
        if not m:
            continue
        market_cap = None
        if cap_col_idx is not None:
            tds = tr.find_all("td")
            if cap_col_idx < len(tds):
                market_cap = _to_number(tds[cap_col_idx].get_text(strip=True))
        rows.append({"code": m.group(1), "name": link.get_text(strip=True), "market_cap": market_cap})
    return rows


def get_market_universe(sosok: int, top_n: int = 200, sleep: float = 0.3) -> list:
    """
    지정한 시장(0=KOSPI, 1=KOSDAQ) 하나에서 시가총액 상위 top_n개 종목을 반환.
    반환: [{"code": ..., "name": ...}, ...] (시가총액 내림차순)
    """
    market_type = MARKET_TYPE_MAP[sosok]
    all_rows = []
    page_size = 100
    start_idx = 0

    while len(all_rows) < top_n:
        try:
            rows = _fetch_via_api(market_type, start_idx, page_size)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 종목 목록 API 호출 중 예외(marketType={market_type}, startIdx={start_idx}): {e}")
            rows = []

        if not rows:
            break
        all_rows.extend(rows)
        start_idx += page_size
        time.sleep(sleep)

    if not all_rows:
        print(f"[WARN] 신규 API로 종목을 하나도 못 가져옴(marketType={market_type}). 예전 HTML 방식으로 재시도.")
        page = 1
        max_page = (top_n // 50) + 2
        while page <= max_page:
            rows = _fetch_via_legacy_html(sosok, page)
            if not rows:
                break
            all_rows.extend(rows)
            page += 1
            time.sleep(sleep)

    df = pd.DataFrame(all_rows).drop_duplicates(subset="code")
    df = df.dropna(subset=["market_cap"])
    df = df.sort_values("market_cap", ascending=False)
    return df.head(top_n)[["code", "name"]].to_dict("records")


def get_top_market_cap_universe(top_n: int = 600, sleep: float = 0.3) -> list:
    """KOSPI + KOSDAQ 전체에서 시가총액 상위 top_n개 종목 코드를 반환."""
    all_codes = []
    for sosok in (0, 1):
        stocks = get_market_universe(sosok, top_n=top_n, sleep=sleep)
        all_codes.extend(s["code"] for s in stocks)
    return all_codes[:top_n]
