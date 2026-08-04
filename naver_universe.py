# -*- coding: utf-8 -*-
"""
네이버 증권 '시가총액' 순위 페이지에서 KOSPI/KOSDAQ 종목 목록(코드/이름/시가총액)을 가져온다.
페이지: https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=N (0=KOSPI, 1=KOSDAQ)

KRX API 없이 스캔 대상 종목을 정하기 위한 용도 (전종목을 다 스캔하면 느리므로
시가총액 상위 위주로 축소).
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
    "Referer": "https://finance.naver.com",
}

SISE_SUM_URL = "https://finance.naver.com/sise/sise_market_sum.naver"


def _parse_page(sosok: int, page: int):
    resp = requests.get(
        SISE_SUM_URL, params={"sosok": sosok, "page": page}, headers=HEADERS, timeout=5
    )
    resp.raise_for_status()
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.select_one("table.type_2")
    if table is None:
        return []

    header_cols = [th.get_text(strip=True) for th in table.select("thead th")]
    cap_col_idx = next((i for i, h in enumerate(header_cols) if "시가총액" in h), None)

    rows = []
    for tr in table.select("tbody tr"):
        link = tr.select_one("a.tltle")
        if link is None:
            continue
        href = link.get("href", "")
        m = re.search(r"code=(\d{6})", href)
        if not m:
            continue
        code = m.group(1)
        name = link.get_text(strip=True)

        market_cap = None
        if cap_col_idx is not None:
            tds = tr.find_all("td")
            if cap_col_idx < len(tds):
                cap_txt = tds[cap_col_idx].get_text(strip=True).replace(",", "")
                try:
                    market_cap = float(cap_txt)
                except ValueError:
                    market_cap = None

        rows.append({"code": code, "name": name, "market_cap": market_cap})
    return rows


def get_top_market_cap_universe(top_n: int = 600, sleep: float = 0.3) -> list:
    """
    KOSPI(sosok=0) + KOSDAQ(sosok=1) 전체에서 시가총액 상위 top_n개 종목 코드를 반환.
    각 시장에서 넉넉히(top_n에 맞춰) 페이지를 가져온 뒤 합쳐서 재정렬한다.
    """
    all_rows = []
    for sosok in (0, 1):
        page = 1
        max_page = (top_n // 50) + 2
        while page <= max_page:
            rows = _parse_page(sosok, page)
            if not rows:
                break
            all_rows.extend(rows)
            page += 1
            time.sleep(sleep)

    df = pd.DataFrame(all_rows).drop_duplicates(subset="code")
    df = df.dropna(subset=["market_cap"])
    df = df.sort_values("market_cap", ascending=False)
    return df.head(top_n)["code"].tolist()


def get_market_universe(sosok: int, top_n: int = 200, sleep: float = 0.3) -> list:
    """
    지정한 시장(0=KOSPI, 1=KOSDAQ) 하나에서 시가총액 상위 top_n개 종목을 반환.
    반환: [{"code": ..., "name": ...}, ...] (시가총액 내림차순)
    """
    all_rows = []
    page = 1
    max_page = (top_n // 50) + 2
    while page <= max_page:
        rows = _parse_page(sosok, page)
        if not rows:
            break
        all_rows.extend(rows)
        page += 1
        time.sleep(sleep)

    df = pd.DataFrame(all_rows).drop_duplicates(subset="code")
    df = df.dropna(subset=["market_cap"])
    df = df.sort_values("market_cap", ascending=False)
    return df.head(top_n)[["code", "name"]].to_dict("records")
