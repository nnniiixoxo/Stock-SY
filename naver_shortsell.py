# -*- coding: utf-8 -*-
"""
네이버 증권 '공매도 현황' 페이지에서 종목별 일별 공매도 거래량을 가져오는 모듈.
페이지: https://finance.naver.com/item/short_trade.naver?code=XXXXXX&page=N

(네이버가 표시하는 공매도 데이터는 KRX 원본을 다시 서빙하는 것이라, 이 페이지 자체는
frgn.naver와 같은 구조의 일반 웹페이지라서 KRX 직접 접속 차단 문제와는 무관하다.)
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

SHORT_TRADE_URL = "https://finance.naver.com/item/short_trade.naver"


def get_short_sell_volume(code: str, days: int = 15, sleep: float = 0.3) -> pd.DataFrame:
    """
    최근 `days` 거래일치 공매도 거래량을 반환.
    반환 컬럼: date, short_volume  (날짜 오름차순)
    페이지 구조 변경 등으로 파싱에 실패하면 빈 DataFrame을 반환한다 (호출부에서 안전하게 처리).
    """
    rows = []
    page = 1
    max_page = (days // 10) + 2
    header_cols = None

    try:
        while len(rows) < days and page <= max_page:
            resp = requests.get(
                SHORT_TRADE_URL, params={"code": code, "page": page}, headers=HEADERS, timeout=5
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
                if len(tds) < 3:
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

                # 컬럼명이 "공매도량", "공매도거래량" 등으로 표기될 수 있어 유연하게 탐색.
                # "공매도평균거래량"(비교용 평균) 컬럼과 헷갈리지 않도록 "평균"은 명시적으로 제외.
                vol_key = next(
                    (
                        k for k in values
                        if "공매도" in k and ("량" in k or "수량" in k)
                        and "비중" not in k and "평균" not in k
                    ),
                    None,
                )

                def to_float(v):
                    try:
                        return float(v) if v not in ("", "-") else 0.0
                    except ValueError:
                        return 0.0

                rows.append({"date": date, "short_volume": to_float(values.get(vol_key, "0"))})
            page += 1
            time.sleep(sleep)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] {code} 공매도 데이터 조회 실패: {e}")
        return pd.DataFrame(columns=["date", "short_volume"])

    if not rows:
        return pd.DataFrame(columns=["date", "short_volume"])

    df = pd.DataFrame(rows).drop_duplicates(subset="date").sort_values("date")
    return df.tail(days).reset_index(drop=True)


def is_short_sell_decreasing(df: pd.DataFrame, recent_n: int = 3) -> bool:
    """
    최근 recent_n일 평균 공매도 거래량이, 그 이전 recent_n일 평균보다 낮은지 확인.
    (공매도 압력이 완화되는 추세인지를 보는 것으로, 하루하루 무조건 감소해야 하는
    엄격한 조건보다는 노이즈에 덜 민감하다.)
    """
    needed = recent_n * 2
    recent = df.tail(needed)
    if len(recent) < needed:
        return False
    prior_avg = recent["short_volume"].iloc[:recent_n].mean()
    recent_avg = recent["short_volume"].iloc[recent_n:].mean()
    if prior_avg <= 0:
        return False
    return bool(recent_avg < prior_avg)
