from fixture_pkg.pricing import discounted


def test_bulk_discount() -> None:
    # Deliberately weak: only exercises the qty > 10 branch, so the
    # `<=` → `<` boundary mutant survives (exposed by the deep tier).
    assert discounted(100, 20) == 90.0
