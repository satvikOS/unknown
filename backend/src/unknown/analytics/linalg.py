"""Small dense linear algebra for covariance diagnostics (CAP-PORT-08 PSD/condition checks, CAP-PORT-13; stdlib only).

Covariance matrices here are a few dozen assets at most, so a cyclic Jacobi eigenvalue
iteration is exact enough and dependency free. Statistical floats only."""
from __future__ import annotations

import math

Matrix = list[list[float]]


def is_symmetric(a: Matrix, tol: float = 1e-12) -> bool:
    n = len(a)
    return all(abs(a[i][j] - a[j][i]) <= tol * max(1.0, abs(a[i][j])) for i in range(n) for j in range(i + 1, n))


def symmetric_eigenvalues(a: Matrix, *, tol: float = 1e-14, max_sweeps: int = 100) -> list[float]:
    """Eigenvalues of a real symmetric matrix by the cyclic Jacobi method, ascending."""
    n = len(a)
    m = [row[:] for row in a]
    for _ in range(max_sweeps):
        off = math.fsum(m[i][j] ** 2 for i in range(n) for j in range(n) if i != j)
        if off <= tol * tol * max(1.0, math.fsum(m[i][i] ** 2 for i in range(n))):
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if m[p][q] == 0.0:
                    continue
                theta = (m[q][q] - m[p][p]) / (2.0 * m[p][q])
                t = (1.0 if theta >= 0 else -1.0) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    mkp, mkq = m[k][p], m[k][q]
                    m[k][p] = c * mkp - s * mkq
                    m[k][q] = s * mkp + c * mkq
                for k in range(n):
                    mpk, mqk = m[p][k], m[q][k]
                    m[p][k] = c * mpk - s * mqk
                    m[q][k] = s * mpk + c * mqk
    return sorted(m[i][i] for i in range(n))


def condition_number(eigs: list[float]) -> float | None:
    if not eigs or eigs[0] <= 0:
        return None
    return eigs[-1] / eigs[0]


def matvec(a: Matrix, x: list[float]) -> list[float]:
    return [math.fsum(a[i][j] * x[j] for j in range(len(x))) for i in range(len(a))]


def quad(a: Matrix, x: list[float]) -> float:
    return math.fsum(x[i] * v for i, v in enumerate(matvec(a, x)))
