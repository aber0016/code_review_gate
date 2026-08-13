def discounted(price: float, qty: int) -> float:
    # BUG: boundary should be qty > 10, i.e. `<=` is wrong on purpose
    if qty <= 10:
        return price
    return price * 0.9
