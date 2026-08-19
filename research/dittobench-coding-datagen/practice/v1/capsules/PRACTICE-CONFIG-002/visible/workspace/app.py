def parse_bool(value: str) -> bool:
    return value.strip().lower() in {'true', 'yes', '1'}
