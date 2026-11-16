"""Small, dependency-free statistical helpers.

Deliberately plain Python: the installed environment has no numpy/scipy, and
the analysis unit is a store, so matrix sizes are tiny.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def variance(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / (n - 1)


def sd(values: Sequence[float]) -> float:
    return math.sqrt(variance(values))


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total


def weighted_variance(values: Sequence[float], weights: Sequence[float]) -> float:
    total = sum(weights)
    if total <= 0 or len(values) < 2:
        return 0.0
    m = weighted_mean(values, weights)
    return sum(w * (v - m) ** 2 for v, w in zip(values, weights)) / total


def standardized_mean_difference(
    treated: Sequence[float],
    control: Sequence[float],
    treated_weights: Sequence[float] | None = None,
    control_weights: Sequence[float] | None = None,
) -> float:
    if len(treated) < 1 or len(control) < 1:
        return 0.0
    if treated_weights is None:
        treated_weights = [1.0] * len(treated)
    if control_weights is None:
        control_weights = [1.0] * len(control)
    mt = weighted_mean(treated, treated_weights)
    mc = weighted_mean(control, control_weights)
    vt = weighted_variance(treated, treated_weights)
    vc = weighted_variance(control, control_weights)
    denom = math.sqrt((vt + vc) / 2.0)
    if denom <= 1e-12:
        return 0.0
    return (mt - mc) / denom


def percentile(sorted_values: Sequence[float], q: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def bootstrap_interval(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    ordered = sorted(values)
    return percentile(ordered, 0.025), percentile(ordered, 0.975)


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


def solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small systems only)."""
    n = len(matrix)
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            aug[col][col] += 1e-8
            pivot = col
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        divisor = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= divisor
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0.0:
                continue
            for j in range(col, n + 1):
                aug[r][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


def fit_logistic(
    rows: Sequence[Sequence[float]],
    target: Sequence[int],
    ridge: float = 1.0,
    iterations: int = 60,
) -> list[float]:
    """Ridge-penalised logistic regression via IRLS.

    `rows` must already include an intercept column.  The intercept is not
    penalised.
    """
    if not rows:
        return []
    p = len(rows[0])
    beta = [0.0] * p
    for _ in range(iterations):
        xtwx = [[0.0] * p for _ in range(p)]
        grad = [0.0] * p
        for x, y in zip(rows, target):
            eta = sum(xi * bi for xi, bi in zip(x, beta))
            mu = sigmoid(max(-30.0, min(30.0, eta)))
            w = max(mu * (1.0 - mu), 1e-8)
            residual = y - mu
            for i in range(p):
                grad[i] += x[i] * residual
                if x[i] == 0.0:
                    continue
                wx = w * x[i]
                for j in range(p):
                    xtwx[i][j] += wx * x[j]
        for i in range(p):
            if i > 0:
                grad[i] -= ridge * beta[i]
                xtwx[i][i] += ridge
        step = solve_linear(xtwx, grad)
        if any(not math.isfinite(v) for v in step):
            break
        magnitude = max(abs(v) for v in step)
        if magnitude > 5.0:
            scale = 5.0 / magnitude
            step = [v * scale for v in step]
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(v) for v in step) < 1e-8:
            break
    return beta


def predict_logistic(rows: Sequence[Sequence[float]], beta: Sequence[float]) -> list[float]:
    out = []
    for x in rows:
        eta = sum(xi * bi for xi, bi in zip(x, beta))
        out.append(sigmoid(max(-30.0, min(30.0, eta))))
    return out


def logit(probability: float) -> float:
    p = min(1.0 - 1e-6, max(1e-6, probability))
    return math.log(p / (1.0 - p))


def standardized_columns(
    records: Sequence[dict[str, Any]], columns: Sequence[str]
) -> tuple[list[list[float]], dict[str, tuple[float, float]]]:
    stats: dict[str, tuple[float, float]] = {}
    for column in columns:
        values = [float(r[column]) for r in records]
        m = mean(values)
        s = sd(values)
        stats[column] = (m, s if s > 1e-12 else 1.0)
    matrix = []
    for r in records:
        matrix.append(
            [(float(r[c]) - stats[c][0]) / stats[c][1] for c in columns]
        )
    return matrix, stats
