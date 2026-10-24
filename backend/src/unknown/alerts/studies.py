"""Chart studies for alert rules: the same definitions as the terminal's chart studies (``chart.studies@1``,
terminal/src/workspaces/chart/studies.ts), so an alert on 'RSI 14 above 70' means what the chart shows.

Values are IEEE doubles (display numbers, never money). Each function returns the LAST value of the series, or None
while the study is warming up.

* SMA_n(t) = mean(x[t-n+1..t]).
* EMA_n: alpha = 2/(n+1), seeded with SMA_n at t = n-1.
* RSI_n (Wilder): first average gain/loss = simple mean of the first n changes, then avg = (avg x (n-1) + cur) / n;
  RSI = 100 - 100/(1+RS); average loss 0 gives 100 (50 when both are 0).
"""
from __future__ import annotations

STUDY_VERSION = "chart.studies@1"
STUDIES = ("sma", "ema", "rsi")


def sma(x: list[float], n: int) -> float | None:
    if n < 1 or len(x) < n:
        return None
    return sum(x[-n:]) / n


def ema(x: list[float], n: int) -> float | None:
    if n < 1 or len(x) < n:
        return None
    alpha = 2 / (n + 1)
    prev = sum(x[:n]) / n
    for v in x[n:]:
        prev = alpha * v + (1 - alpha) * prev
    return prev


def rsi(x: list[float], n: int) -> float | None:
    if n < 1 or len(x) <= n:
        return None
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = x[i] - x[i - 1]
        if d > 0:
            gain += d
        else:
            loss -= d
    gain /= n
    loss /= n
    for i in range(n + 1, len(x)):
        d = x[i] - x[i - 1]
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return 100 - 100 / (1 + gain / loss)


def value(study: str, closes: list[float], n: int) -> float | None:
    if study == "sma":
        return sma(closes, n)
    if study == "ema":
        return ema(closes, n)
    if study == "rsi":
        return rsi(closes, n)
    raise ValueError(f"unknown study {study!r}; one of {STUDIES}")
