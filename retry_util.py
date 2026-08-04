# -*- coding: utf-8 -*-
"""
KRX 서버가 가끔 빈 응답을 주는 문제 때문에,
실패 시 잠깐 대기 후 재시도하는 공용 헬퍼.
"""
import time


def retry_call(func, *args, retries: int = 5, delay: float = 3.0, **kwargs):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            result = func(*args, **kwargs)
            if result is not None and not (hasattr(result, "empty") and result.empty):
                return result
            last_err = ValueError("빈 응답(empty response)")
        except Exception as e:  # noqa: BLE001
            last_err = e
        print(f"[재시도 {attempt}/{retries}] {getattr(func, '__name__', func)} 실패: {last_err}")
        time.sleep(delay)
    raise RuntimeError(
        f"{getattr(func, '__name__', func)} 계속 실패함 (마지막 에러: {last_err})"
    )
