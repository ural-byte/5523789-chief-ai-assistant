from decimal import Decimal
from pathlib import Path

from app.pricing import Pricing, Usage, normalize_usage


def test_categories_without_double_counting():
    pricing = Pricing(Path("config/pricing.json"))
    usage = normalize_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 200},
            "completion_tokens_details": {"reasoning_tokens": 80},
            "tool_tokens": 60,
        }
    )
    cost, complete = pricing.calculate(usage, pricing.snapshot("gpt://folder/deepseek-v4-flash"))
    assert cost == Decimal("0.305")
    assert complete
    assert usage.extras["completion_tokens_details.reasoning_tokens"] == 80


def test_unknown_usage_and_embedding_single_charge():
    pricing = Pricing(Path("config/pricing.json"))
    assert normalize_usage(None).input_tokens is None
    snapshot = pricing.snapshot("emb://folder/text-embeddings-v2-doc/")
    usage = normalize_usage({"prompt_tokens": 100, "total_tokens": 100}, True)
    assert pricing.calculate(usage, snapshot, True) == (Decimal("0.00101"), True)
    cost, complete = pricing.calculate(
        Usage(input_tokens=100, output_tokens=20), pricing.snapshot("deepseek-v4-flash")
    )
    assert cost is not None and not complete
    assert pricing.calculate(Usage(), pricing.snapshot("deepseek-v4-flash")) == (None, False)


def test_configured_latest_aliases_and_selected_v4_1_rates():
    pricing = Pricing(Path("config/pricing.json"))
    usage = Usage(input_tokens=1000, output_tokens=100, cached_tokens=200)
    assert pricing.calculate(usage, pricing.snapshot("gpt://folder/deepseek-v4.1-flash")) == (
        Decimal("0.305"),
        True,
    )
    assert pricing.calculate(
        Usage(embedding_tokens=100),
        pricing.snapshot("emb://folder/text-embeddings-v2-doc/latest"),
        True,
    ) == (Decimal("0.00101"), True)
    assert pricing.snapshot("gpt://folder/unrecognised-model/latest")["rates"] is None
    # The live generation gateway reports a folderless model URI.
    assert pricing.calculate(usage, pricing.snapshot("gpt://deepseek-v4.1-flash/latest")) == (
        Decimal("0.305"),
        True,
    )
