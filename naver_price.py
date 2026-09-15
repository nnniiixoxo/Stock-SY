# -*- coding: utf-8 -*-
"""
종목별 일봉 시세(OHLCV)를 가져오는 모듈.

2026-09 시점 상태:
naver_universe.py와 같은 이유로, 네이버 증권의 예전 HTML 일별시세 페이지
(finance.naver.com/item/sise_day.naver)가 이제 자바스크립트 렌더링 방식으로
바뀌어서 requests로는 데이터를 못 읽어오게 됐다. 그래서 새 정식 API로 전환했다:
  GET https://stock.naver.com/api/domestic/detail/{itemCode}/siseDay
      ?pageSize={n}&bizdate={yyyyMMdd}
이 API도 비공식/미문서화 상태라 정확한 응답 필드명을 100% 확신할 수 없어서,
여러 후보 키를 순서대로 시도하고, 실패하면 실제 응답 구조를 로그로 남긴다.
API가 완전히 실패하면 예전 HTML 방식으로도 한 번 더 시도한다.
"""
import time
import datetime
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

SISE_DAY_API_TMPL = "https://stock.naver.com/api/domestic/detail/{item_code}/siseDay"
SISE_DAY_HTML_URL = "https://finance.naver.com/item/sise_day.naver"

LIST_KEY_CANDIDATES = ("siseDays", "siseDayList", "items", "list", "content", "data", "result")
DATE_KEY_CANDIDATES = ("bizdate", "bizDate", "date", "localDate", "tradeDate")
CLOSE_KEY_CANDIDATES = ("closeprice", "closePrice", "close", "ncv")
OPEN_KEY_CANDIDATES = ("openprice", "openPrice", "open")
HIGH_KEY_CANDIDATES = ("highprice", "highPrice", "high")
LOW_KEY_CANDIDATES = ("lowprice", "lowPrice", "low")
VOLUME_KEY_CANDIDATES = (
    "accumulatedtradingvolume", "accumulatedTradingVolume", "volume", "quant", "tradingvolume",
)

_DIAG_PRINTED = False  # 진단 로그가 너무 많이 찍히지 않도록 실행당 한 번만 남김


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
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in LIST_KEY_CANDIDATES:
            val = data.get(key)
            if isinstance(val, list):
                return val
    return None


def _row_from_item(item: dict):
    date_raw = next((item[k] for k in DATE_KEY_CANDIDATES if item.get(k)), None)
    close = next((item[k] for k in CLOSE_KEY_CANDIDATES if item.get(k) is not None), None)
    open_ = next((item[k] for k in OPEN_KEY_CANDIDATES if item.get(k) is not None), None)
    high = next((item[k] for k in HIGH_KEY_CANDIDATES if item.get(k) is not None), None)
    low = next((item[k] for k in LOW_KEY_CANDIDATES if item.get(k) is not None), None)
    volume = next((item[k] for k in VOLUME_KEY_CANDIDATES if item.get(k) is not None), None)

    if date_raw is None or close is None:
        return None

    date_str = str(date_raw).replace(".", "").replace("-", "")[:8]
    try:
        date = pd.to_datetime(date_str, format="%Y%m%d")
    except ValueError:
        return None

    close_n = _to_number(close)
    if close_n is None:
        return None

    return {
        "date": date,
        "close": close_n,
        "open": _to_number(open_) if open_ is not None else close_n,
        "high": _to_number(high) if high is not None else close_n,
        "low": _to_number(low) if low is not None else close_n,
        "volume": _to_number(volume) if volume is not None else None,
    }


def _fetch_via_api(code: str, days: int, sleep: float) -> list:
    global _DIAG_PRINTED
    rows_by_date = {}
    bizdate = None
    guard = 0
    call_count = 0
    max_calls = (days // 15) + 5  # pageSize 대략 20 안팎으로 가정하고 여유있게 반복 횟수 제한
    is_first_diag_target = not _DIAG_PRINTED
    prev_oldest = None

    while len(rows_by_date) < days and guard < max_calls:
        params = {"pageSize": 20}
        if bizdate:
            params["bizdate"] = bizdate
        url = SISE_DAY_API_TMPL.format(item_code=code)
        resp = requests.get(url, params=params, headers=HEADERS, timeout=8)
        call_count += 1
        if guard == 0 and is_first_diag_target:
            print(f"[진단] 일별시세 API({code}) 응답 상태코드: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

        items = _extract_items(data)
        if items is None:
            if guard == 0 and is_first_diag_target:
                top_keys = list(data.keys()) if isinstance(data, dict) else f"(list, 길이 {len(data)})"
                print(
                    f"[WARN] 일별시세 API({code}) 응답에서 리스트를 못 찾음. "
                    f"최상위 키: {top_keys}, 응답 앞부분: {str(data)[:500]}"
                )
            break

        new_rows = []
        for it in items:
            if not isinstance(it, dict):
                continue
            row = _row_from_item(it)
            if row:
                new_rows.append(row)

        if guard == 0 and is_first_diag_target:
            if new_rows:
                print(f"[진단] 일별시세 API({code}) 첫 응답 파싱 성공: {len(new_rows)}개 (bizdate 파라미터={bizdate})")
                if items:
                    print(f"[진단] 일별시세 API({code}) 첫 원본 항목 전체 내용(필드명 확인용): {items[0]}")
                zero_vol_count = sum(1 for r in new_rows if not r["volume"])
                if zero_vol_count == len(new_rows):
                    print(
                        f"[WARN] 일별시세 API({code}) 거래량(volume)이 전부 0/누락으로 처리됨 "
                        f"-> VOLUME_KEY_CANDIDATES에 맞는 필드를 못 찾았을 가능성 높음. "
                        f"위 '첫 원본 항목 전체 내용'에서 거래량에 해당하는 실제 키를 확인할 것."
                    )
            elif items:
                print(f"[WARN] 일별시세 API({code}) 항목은 있지만 필드 매칭 실패. 첫 항목 전체 내용: {items[0]}")

        if not new_rows:
            break

        oldest = min(r["date"] for r in new_rows)
        for r in new_rows:
            rows_by_date[r["date"]] = r

        # 이전 호출보다 더 과거로 진행되지 않으면(예: bizdate 파라미터가 기대와 다르게 동작해서
        # 같은 구간을 반복 반환하는 경우) 무한/무의미 반복을 막기 위해 중단한다.
        if prev_oldest is not None and oldest >= prev_oldest:
            if is_first_diag_target:
                print(
                    f"[WARN] 일별시세 API({code}) 페이지네이션이 더 과거로 진행되지 않음 "
                    f"(이전 oldest={prev_oldest}, 이번 oldest={oldest}) -> 중단, 누적 {len(rows_by_date)}개"
                )
            break
        prev_oldest = oldest

        next_bizdate = (oldest - datetime.timedelta(days=1)).strftime("%Y%m%d")
        bizdate = next_bizdate
        guard += 1
        time.sleep(sleep)

    if is_first_diag_target:
        print(f"[진단] 일별시세 API({code}) 최종 결과: {len(rows_by_date)}개 확보 ({call_count}번 호출)")
        _DIAG_PRINTED = True

    return list(rows_by_date.values())


def _fetch_via_legacy_html(code: str, days: int, sleep: float) -> list:
    """예전 HTML 페이지 방식 (2026-09 기준 더 이상 동작하지 않을 가능성이 높지만 최후 수단으로 유지)."""
    rows = []
    page = 1
    max_page = (days // 10) + 2

    while len(rows) < days and page <= max_page:
        resp = requests.get(
            SISE_DAY_HTML_URL,
            params={"code": code, "page": page},
            headers={**HEADERS, "Accept": "text/html"},
            timeout=5,
        )
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")

        trs = soup.select("table.type2 tr[onmouseover]")
        if not trs:
            break

        for tr in trs:
            tds = tr.find_all("td")
            if len(tds) < 7:
                continue
            date_txt = tds[0].get_text(strip=True)
            if not date_txt:
                continue
            try:
                date = pd.to_datetime(date_txt, format="%Y.%m.%d")
                close = float(tds[1].get_text(strip=True).replace(",", ""))
                open_ = float(tds[3].get_text(strip=True).replace(",", ""))
                high = float(tds[4].get_text(strip=True).replace(",", ""))
                low = float(tds[5].get_text(strip=True).replace(",", ""))
                volume = float(tds[6].get_text(strip=True).replace(",", ""))
            except (ValueError, IndexError):
                continue
            rows.append(
                {"date": date, "close": close, "open": open_, "high": high, "low": low, "volume": volume}
            )
        page += 1
        time.sleep(sleep)

    return rows


def get_daily_ohlcv(code: str, days: int = 40, sleep: float = 0.3) -> pd.DataFrame:
    """
    최근 `days` 거래일치 OHLCV를 반환.
    반환 DataFrame 컬럼: date, close, open, high, low, volume  (날짜 오름차순)
    """
    rows = []
    try:
        rows = _fetch_via_api(code, days, sleep)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] {code} 일별시세 API 호출 중 예외: {e}")

    if not rows:
        rows = _fetch_via_legacy_html(code, days, sleep)

    if not rows:
        return pd.DataFrame(columns=["date", "close", "open", "high", "low", "volume"])

    df = pd.DataFrame(rows).drop_duplicates(subset="date").sort_values("date")
    return df.tail(days).reset_index(drop=True)


def get_stock_name(code: str) -> str:
    """네이버 종목 메인 페이지에서 종목명을 가져온다 (실패 시 코드 반환)."""
    try:
        resp = requests.get(
            f"https://finance.naver.com/item/main.naver?code={code}",
            headers=HEADERS,
            timeout=5,
        )
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")
        tag = soup.select_one("div.wrap_company h2 a")
        if tag:
            return tag.get_text(strip=True)
    except requests.RequestException:
        pass
    return code
