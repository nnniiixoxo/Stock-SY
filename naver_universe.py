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

★ 확인된 한계: 이 엔드포인트는 실제로 확인해보니 시장당 시가총액 상위 약 99개까지만
  응답하고, startIdx를 그 이상으로 올려도 명확하게 빈 배열([])을 반환한다(에러나 차단이
  아니라 정상적인 200 응답으로 "더 없음"이라고 답하는 것). 이름 그대로 "시장 요약(default)"용
  엔드포인트라 "전 종목 목록"용으로 설계되지 않은 것으로 추정된다. 그래서 현재는 이 상한선
  (시장당 약 99개, 총 약 198개)을 실질적인 스캔 범위로 받아들이고 있다. 코스피/코스닥
  전 종목(800개+/1500개+)이 필요해지면, 이 엔드포인트가 아닌 별도의 "종목 마스터 목록"
  데이터 소스를 새로 찾아야 한다.
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
CODE_KEY_CANDIDATES = ("itemcode", "itemCode", "code", "stockCode", "symbol")
NAME_KEY_CANDIDATES = ("itemname", "stockName", "itemName", "name", "korName")
CAP_KEY_CANDIDATES = ("marketsum", "marketvalue", "marketcap", "marketSum", "marketCap", "marketValue", "marketSumFormatted", "amount")


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
    is_diag_call = start_idx == 0 or start_idx == page_size  # 첫 페이지 + 두번째 페이지까지는 진단 로그 남김
    if is_diag_call:
        print(f"[진단] 종목 목록 API(marketType={market_type}, startIdx={start_idx}) 응답 상태코드: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()

    items = _extract_items(data)
    if items is None:
        if is_diag_call:
            top_keys = list(data.keys()) if isinstance(data, dict) else f"(list, 길이 {len(data)})"
            print(
                f"[WARN] 종목 목록 API(marketType={market_type}, startIdx={start_idx}) 응답에서 리스트를 못 찾음. "
                f"최상위 키: {top_keys}, 응답 앞부분: {str(data)[:500]}"
            )
        return []

    if is_diag_call and len(items) == 0:
        # 리스트 자체는 찾았지만 비어있는 경우 (예: 정말 더 이상 데이터가 없거나, startIdx 방식이
        # 이 API에서 기대와 다르게 동작하는 경우). 전체 응답을 그대로 남겨서 원인을 확인한다.
        print(
            f"[진단] 종목 목록 API(marketType={market_type}, startIdx={start_idx}) 목록은 찾았지만 비어있음(길이 0). "
            f"전체 응답: {str(data)[:1000]}"
        )

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
    if is_diag_call and rows:
        cap_missing = sum(1 for r in rows if r["market_cap"] is None)
        print(f"[진단] 종목 목록 API(marketType={market_type}, startIdx={start_idx}) 파싱 성공: {len(rows)}개 (누락 {dropped}개)")
        if cap_missing > len(rows) * 0.5 and items:
            sample = items[0]
            print(
                f"[WARN] market_cap 필드를 대부분 못 찾음({cap_missing}/{len(rows)}개, marketType={market_type}). "
                f"첫 항목 전체 내용: {sample}"
            )
        # 응답 최상위에 페이지네이션 관련 필드(totalCount, hasNext 등)가 있는지 확인.
        # (있다면 startIdx를 단순히 더하는 대신 그 필드를 활용해야 할 수 있음)
        if isinstance(data, dict):
            non_list_fields = {k: v for k, v in data.items() if not isinstance(v, list)}
            if non_list_fields:
                print(f"[진단] 종목 목록 API(marketType={market_type}, startIdx={start_idx}) 응답 최상위의 비-목록 필드: {non_list_fields}")
    elif is_diag_call and not rows and items:
        sample = items[0] if items else {}
        print(
            f"[WARN] 종목 목록 API(marketType={market_type}) 항목은 있지만 필드 매칭 실패. "
            f"첫 항목 전체 내용: {sample}"
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

        if start_idx > 0 and len(rows) == 0:
            print(
                f"[진단] 종목 목록 API(marketType={market_type}, startIdx={start_idx})에서 빈 결과 -> "
                f"여기서 페이지네이션 종료 (지금까지 누적 {len(all_rows)}개)"
            )

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

    if not all_rows:
        print(f"[WARN] 모든 방식이 실패해서 종목을 하나도 못 가져옴(marketType={market_type}).")
        return []

    # API 요청 자체가 orderType=marketSum(시가총액순)이라, 응답으로 온 순서 자체가
    # 이미 시가총액 내림차순이다. market_cap 필드명을 못 맞춰서 값을 못 읽어도
    # (필드명이 또 바뀌었을 경우) 이 원래 순서를 그대로 보존해서 쓸 수 있게 해둔다.
    for idx, row in enumerate(all_rows):
        row["_api_order"] = idx

    df = pd.DataFrame(all_rows).drop_duplicates(subset="code")
    cap_found_ratio = df["market_cap"].notna().mean() if len(df) else 0

    if cap_found_ratio < 0.5:
        print(
            f"[WARN] 시가총액(market_cap) 필드를 대부분 못 찾음(matched={cap_found_ratio:.0%}, marketType={market_type}). "
            f"API가 이미 시가총액순으로 준 원래 순서를 대신 사용함. "
            f"첫 종목 원본 예시는 위 [WARN] 필드 매칭 실패 로그를 참고."
        )
        df = df.sort_values("_api_order", ascending=True)
    else:
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
