def allocate_cents(total: int, parties: int) -> list[int]:
    share = total // parties
    return [share] * parties
