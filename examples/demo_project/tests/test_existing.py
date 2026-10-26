from demo_calc import discount_total, mean


def test_existing_discount():
    assert discount_total(100, 0.2) == 80


def test_existing_mean():
    assert mean([2, 4]) == 3
