def cache_key(namespace: str, item: str) -> str:
    return f'{namespace.strip()}:{item.strip()}'
