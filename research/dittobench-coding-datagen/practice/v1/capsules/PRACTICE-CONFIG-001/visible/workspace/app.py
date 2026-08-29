def merge_config(defaults: dict, environment: dict) -> dict:
    result = dict(environment)
    result.update(defaults)
    return result
