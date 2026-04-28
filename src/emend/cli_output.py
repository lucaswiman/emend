import json


def format_json(data: object) -> str:
    return json.dumps(data, indent=2)


def emit_json(data: object) -> None:
    print(format_json(data))
