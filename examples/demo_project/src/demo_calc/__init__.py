"""Small demonstration target with intentionally seeded bugs, not a real bug-finding claim."""


def discount_total(amount, rate):
    if not isinstance(amount, (int, float)) or not isinstance(rate, (int, float)):
        raise TypeError("amount and rate must be numbers")
    if amount < 0 or rate < 0 or rate >= 1:  # Seeded boundary defect: rate == 1 is valid.
        raise ValueError("amount or rate out of range")
    return amount * (1 - rate)


def mean(values):
    if values is None:
        raise TypeError("values must be a numeric sequence")
    return sum(values) / len(values)  # Seeded empty-input defect: should raise ValueError.
