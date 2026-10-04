import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    tool_tokens: int | None = None
    embedding_tokens: int | None = None
    extras: dict = field(default_factory=dict)


def normalize_usage(raw: dict | None, embedding: bool = False) -> Usage:
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("invalid usage")
    raw = raw if isinstance(raw, dict) else {}

    def number(value):
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    details = raw.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    if not isinstance(details, dict):
        raise ValueError("invalid usage details")
    extras = {}

    def walk(values, prefix=""):
        for key, value in values.items():
            if isinstance(value, dict):
                walk(value, prefix + key + ".")
            elif number(value) is not None:
                extras[prefix + key] = value

    walk(raw)
    prompt = number(raw.get("prompt_tokens", raw.get("input_tokens")))
    return Usage(
        input_tokens=None if embedding else prompt,
        output_tokens=number(raw.get("completion_tokens", raw.get("output_tokens"))),
        cached_tokens=number(details.get("cached_tokens", raw.get("cached_tokens"))),
        tool_tokens=number(raw.get("tool_tokens")),
        embedding_tokens=(number(raw.get("total_tokens", prompt)) if embedding else None),
        extras=extras,
    )


class Pricing:
    def __init__(self, path: Path):
        self.config = json.loads(path.read_text())

    def snapshot(self, model: str) -> dict:
        suffix = model.split("/", 3)[-1] if model.startswith(("gpt://", "emb://")) else model
        return {**self.config, "rates": self.config["models"].get(suffix)}

    def calculate(self, usage: Usage, snapshot: dict, embedding=False):
        rates = snapshot["rates"]
        if rates is None:
            return None, False
        unit = Decimal(snapshot["unit"])
        if embedding:
            if usage.embedding_tokens is None:
                return None, False
            return Decimal(usage.embedding_tokens) * Decimal(rates["embedding"]) / unit, True
        cached = usage.cached_tokens
        if cached is not None and usage.input_tokens is not None and cached > usage.input_tokens:
            return None, False
        # Unknown cache split cannot be treated as zero cache: only known components count.
        cost = None
        if usage.output_tokens is not None:
            cost = Decimal(usage.output_tokens) * Decimal(rates["output"])
        if usage.input_tokens is not None and cached is not None:
            input_cost = Decimal(usage.input_tokens - cached) * Decimal(rates["input"])
            input_cost += Decimal(cached) * Decimal(rates["cached"])
            cost = (cost or Decimal(0)) + input_cost
        complete = all(
            value is not None
            for value in (usage.input_tokens, usage.output_tokens, usage.cached_tokens)
        )
        return (cost / unit if cost is not None else None), complete
