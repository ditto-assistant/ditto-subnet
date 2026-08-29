def canonical_endpoint(value: str) -> str:
    return value.rstrip('/') + '/'
