# -*- coding: utf-8 -*-
"""
네이버 증권(finance.naver.com)에서 종목별 일봉 시세를 가져오는 모듈.
페이지: https://finance.naver.com/item/sise_day.naver?code=XXXXXX&page=N
"""
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

SISE_DAY_URL = "https://finance.naver.com/item/sise_day.naver"


def get_daily_ohlcv(code: str, days: int = 40, sleep: float = 0.3) -> pd.DataFrame:
    """
    네이버 증권 일별 시세 페이지를 크롤링해 최근 `days` 거래일치 OHLCV를 반환.
    반환 DataFrame 컬럼: date, close, open, high, low, volume  (날짜 오름차순)
    """
    rows = []
    page = 1
    # 한 페이지당 10개 행 -> 필요한 페이지 수 계산 (여유있게 +1)
    max_page = (days // 10) + 2

    while len(rows) < days and page <= max_page:
        resp = requests.get(
            SISE_DAY_URL, params={"code": code, "page": page}, headers=HEADERS, timeout=5
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
                {
                    "date": date,
                    "close": close,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "volume": volume,
                }
            )
        page += 1
        time.sleep(sleep)  # 네이버 서버 부하 방지용 딜레이

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
