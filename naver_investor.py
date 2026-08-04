# -*- coding: utf-8 -*-
"""
네이버 증권 '외국인/기관 매매 동향' 페이지에서
종목별 일별 외국인/기관 순매매(수량)를 가져오는 모듈.
페이지: https://finance.naver.com/item/frgn.naver?code=XXXXXX&page=N

주의: 이 페이지는 순매매를 '금액'이 아니라 '주식 수량'으로 제공합니다.
KRX 데이터(금액 기준)와 다른 단위이지만, 조건 판단(양수/음수, 즉 순매수 여부)에는
문제가 없습니다.
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

FRGN_URL = "https://finance.naver.com/item/frgn.naver"


def get_foreign_institution_net(code: str, days: int = 15, sleep: float = 0.3) -> pd.DataFrame:
    """
    최근 `days` 거래일치 외국인/기관 순매매(수량)를 반환.
    반환 컬럼: date, foreign_net, institution_net  (날짜 오름차순)
    """
    rows = []
    page = 1
    max_page = (days // 10) + 2
    header_cols = None

    while len(rows) < days and page <= max_page:
        resp = requests.get(
            FRGN_URL, params={"code": code, "page": page}, headers=HEADERS, timeout=5
        )
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")

        table = soup.select_one("table.type2")
        if table is None:
            break

        if header_cols is None:
            header_cols = [th.get_text(strip=True) for th in table.select("thead th")]

        trs = table.select("tr[onmouseover]")
        if not trs:
            break

        for tr in trs:
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            date_txt = tds[0].get_text(strip=True)
            if not date_txt:
                continue
            try:
                date = pd.to_datetime(date_txt, format="%Y.%m.%d")
            except ValueError:
                continue

            values = {}
            for name, td in zip(header_cols, tds):
                values[name] = td.get_text(strip=True).replace(",", "")

            inst_key = next((k for k in values if "기관" in k), None)
            foreign_key = next(
                (k for k in values if k.startswith("외국인") and "보유" not in k and "지분" not in k),
                None,
            )

            def to_float(v):
                try:
                    return float(v) if v not in ("", "-") else 0.0
                except ValueError:
                    return 0.0

            rows.append(
                {
                    "date": date,
                    "institution_net": to_float(values.get(inst_key, "0")),
                    "foreign_net": to_float(values.get(foreign_key, "0")),
                }
            )
        page += 1
        time.sleep(sleep)

    if not rows:
        return pd.DataFrame(columns=["date", "foreign_net", "institution_net"])

    df = pd.DataFrame(rows).drop_duplicates(subset="date").sort_values("date")
    return df.tail(days).reset_index(drop=True)


def has_3day_consecutive_net_buy(df: pd.DataFrame) -> bool:
    """최근 3거래일 동안 외국인 또는 기관 중 하나라도 매일 순매수(>0)인지 확인."""
    recent = df.tail(3)
    if len(recent) < 3:
        return False
    foreign_ok = bool((recent["foreign_net"] > 0).all())
    institution_ok = bool((recent["institution_net"] > 0).all())
    return foreign_ok or institution_ok
