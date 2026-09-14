# -*- coding: utf-8 -*-
"""
네이버 증권 '시가총액' 순위 페이지에서 KOSPI/KOSDAQ 종목 목록(코드/이름/시가총액)을 가져온다.
페이지: https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=N (0=KOSPI, 1=KOSDAQ)
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


def _find_market_table(soup: BeautifulSoup):
    """
    시가총액 순위 표를 찾는다. class="type_2"를 우선 시도하고,
    (네이버가 클래스명을 바꿨을 경우를 대비해) 헤더에 "종목명"과 "시가총액"이 모두
    들어있는 표를 텍스트 기준으로 찾는 방식으로 보완한다.
    """
    table = soup.select_one("table.type_2")
    if table is not None:
        return table, "type_2"

    for table in soup.find_all("table"):
        header_text = table.find("thead")
        header_text = header_text.get_text() if header_text else table.get_text()[:300]
        if "종목명" in header_text and "시가총액" in header_text:
            return table, "text-anchor"

    return None, None


def _parse_page(sosok: int, page: int):
    resp = requests.get(
        SISE_SUM_URL, params={"sosok": sosok, "page": page}, headers=HEADERS, timeout=5
    )
    if page == 1:
        print(f"[진단] 시가총액 페이지(sosok={sosok}) 응답 상태코드: {resp.status_code}")
    resp.raise_for_status()
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "lxml")

    table, matched_by = _find_market_table(soup)
    if table is None:
        if page == 1:
            all_tables = soup.find_all("table")
            table_classes = [t.get("class") for t in all_tables]
            print(
                f"[WARN] 시가총액 페이지(sosok={sosok}) 표를 못 찾음. "
                f"페이지 내 표 개수: {len(all_tables)}, 각 표의 class: {table_classes}"
            )
            print(f"[진단] 응답 본문(앞 1000자): {resp.text[:1000]}")
        return []
    elif page == 1 and matched_by == "text-anchor":
        print(f"[진단] table.type_2 대신 텍스트 기준으로 표를 찾음 (sosok={sosok})")

    header_cols = [th.get_text(strip=True) for th in table.select("thead th")]
    if not header_cols:
        header_cols = [th.get_text(strip=True) for th in table.select("th")]
    cap_col_idx = next((i for i, h in enumerate(header_cols) if "시가총액" in h), None)

    rows = []
    body_rows = table.select("tbody tr") or table.select("tr")
    for tr in body_rows:
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
    """KOSPI + KOSDAQ 전체에서 시가총액 상위 top_n개 종목 코드를 반환."""
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
