def eviction_candidate(entries: list[tuple[str, int]]) -> str:
    return max(entries, key=lambda entry: entry[1])[0]
