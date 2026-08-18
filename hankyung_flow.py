# -*- coding: utf-8 -*-
"""
한국경제(markets.hankyung.com)에서 코스피/코스닥 "시장 전체" 개인/기관/외국인
순매매 금액(억원)을 가져오는 모듈.

네이버는 종목 단위 수급만 제공하고 시장 전체 합계는 제공하지 않아서,
이 페이지를 대신 사용한다. (실험적 기능 — 페이지 구조가 바뀌면 조용히 실패하고
None을 반환하도록 만들어서, 실패해도 나머지 기능에는 영향이 없게 했다.)
"""
import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://markets.hankyung.com/",
}

MARKET_URL = "https://markets.hankyung.com/"


def _extract_market_block(text: str, label: str):
    """text 안에서 '코스피 6,977.94 164.60 +2.42%' 같은 블록을 찾아 파싱."""
    pattern = re.compile(
        re.escape(label) + r"\**\s*([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([+-][\d.]+)%",
        re.S,
    )
    m = pattern.search(text)
    if not m:
        return None

    window = text[m.end() : m.end() + 500]

    def find_amount(tag):
        mm = re.search(tag + r".*?([+-]?[\d,]+)\s*억원", window, re.S)
        return float(mm.group(1).replace(",", "")) if mm else None

    return {
        "value": float(m.group(1).replace(",", "")),
        "change": float(m.group(2).replace(",", "")),
        "rate": float(m.group(3)),
        "personal": find_amount("개인"),
        "institution": find_amount("기관"),
        "foreign": find_amount("외국인"),
    }


def get_market_flow():
    """
    반환: {"KOSPI": {value, change, rate, personal, institution, foreign}, "KOSDAQ": {...}}
    실패 시 None (호출부에서 그냥 이 정보 없이 진행하면 됨).
    """
    try:
        resp = requests.get(MARKET_URL, headers=HEADERS, timeout=8)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text("\n")

        kospi = _extract_market_block(text, "코스피")
        kosdaq = _extract_market_block(text, "코스닥")

        if not kospi and not kosdaq:
            return None

        return {"KOSPI": kospi, "KOSDAQ": kosdaq}
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 한경 시장 수급 조회 실패(페이지 구조 변경일 수 있음): {e}")
        return None
