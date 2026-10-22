"""Walk-forward, purged k-fold and combinatorial purged CV fold generators (CAP-RES-07; BR-DAT-14).

Observations are indexed 0..n-1 in time order. Observation ``i`` carries the
label interval ``[i, i + h]`` (``h`` = label horizon in observations,
including any execution delay the caller folds in). For every contiguous test
run ``[a, b]`` the *test label span* is ``[a, b + h]``:

* purge  P = { i in train : [i, i + h] intersects [a, b + h] }  (both sides of the run)
* embargo E = { i in train : b < i <= b + embargo }              (training that follows a test run)
* effective training set = candidates minus (P union E)

Consequences checked by the property tests: every training observation is
more than ``h`` observations away from every test observation, and no
training observation starts within ``embargo`` observations after a test run.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Fold:
    index: int
    train: tuple[int, ...]
    test: tuple[int, ...]
    purged: tuple[int, ...]
    embargoed: tuple[int, ...]

    def to_json(self) -> dict:
        def span(ix: tuple[int, ...]) -> list:
            return [ix[0], ix[-1]] if ix else []
        return {"index": self.index, "train_span": span(self.train), "train_n": len(self.train), "test_span": span(self.test),
                "test_n": len(self.test), "purged": list(self.purged), "embargoed": list(self.embargoed)}


def _runs(test: list[int]) -> list[tuple[int, int]]:
    runs = []
    for i in sorted(test):
        if runs and i == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], i)
        else:
            runs.append((i, i))
    return runs


def purge_and_embargo(candidates: list[int], test: list[int], horizon: int, embargo: int) -> tuple[list[int], list[int], list[int]]:
    if horizon < 0 or embargo < 0:
        raise ValueError("horizon and embargo must be >= 0")
    test_set = set(test)
    runs = _runs(test)
    train, purged, embargoed = [], [], []
    for i in sorted(set(candidates) - test_set):
        if any(a - horizon <= i <= b + horizon for a, b in runs):
            purged.append(i)
        elif any(b < i <= b + embargo for _, b in runs):
            embargoed.append(i)
        else:
            train.append(i)
    return train, purged, embargoed


def walk_forward(n: int, *, train_size: int, test_size: int, horizon: int, embargo: int = 0, expanding: bool = False,
                 min_test: int = 1) -> list[Fold]:
    """Train on the past only, evaluate on the next unseen segment, advance (book ch. 7, A06)."""
    if min(train_size, test_size) < 1 or n < train_size + 1:
        raise ValueError("invalid walk-forward sizes")
    folds = []
    start = train_size
    while start < n:
        end = min(n, start + test_size)
        if end - start < min_test:
            break
        cand = list(range(0 if expanding else max(0, start - train_size), start))
        test = list(range(start, end))
        train, purged, emb = purge_and_embargo(cand, test, horizon, embargo)
        if train:
            folds.append(Fold(len(folds), tuple(train), tuple(test), tuple(purged), tuple(emb)))
        start = end
    return folds


def _groups(n: int, k: int) -> list[list[int]]:
    if k < 2 or n < k:
        raise ValueError("need 2 <= groups <= n")
    bounds = [round(g * n / k) for g in range(k + 1)]
    return [list(range(bounds[g], bounds[g + 1])) for g in range(k)]


def purged_kfold(n: int, k: int, *, horizon: int, embargo: int = 0) -> list[Fold]:
    folds = []
    groups = _groups(n, k)
    for g, test in enumerate(groups):
        cand = [i for j, grp in enumerate(groups) if j != g for i in grp]
        train, purged, emb = purge_and_embargo(cand, test, horizon, embargo)
        folds.append(Fold(g, tuple(train), tuple(test), tuple(purged), tuple(emb)))
    return folds


def cpcv(n: int, groups: int, test_groups: int, *, horizon: int, embargo: int = 0) -> list[Fold]:
    """Combinatorial purged CV: every combination of ``test_groups`` of ``groups`` contiguous groups."""
    if not 1 <= test_groups < groups:
        raise ValueError("need 1 <= test_groups < groups")
    grp = _groups(n, groups)
    folds = []
    for combo in itertools.combinations(range(groups), test_groups):
        test = [i for g in combo for i in grp[g]]
        cand = [i for g in range(groups) if g not in combo for i in grp[g]]
        train, purged, emb = purge_and_embargo(cand, test, horizon, embargo)
        folds.append(Fold(len(folds), tuple(train), tuple(test), tuple(purged), tuple(emb)))
    return folds


def cpcv_split_count(groups: int, test_groups: int) -> int:
    return math.comb(groups, test_groups)


def cpcv_path_count(groups: int, test_groups: int) -> int:
    """Number of complete OOS paths = k x C(N, k) / N."""
    return test_groups * math.comb(groups, test_groups) // groups
