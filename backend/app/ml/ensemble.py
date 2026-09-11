def weighted_average(parts: list[tuple[float | None, float]]) -> float | None:
    valid = [(p, w) for p, w in parts if p is not None and w > 0]
    total = sum(w for _, w in valid)
    if total <= 0:
        return None
    return sum(float(p) * w for p, w in valid) / total

