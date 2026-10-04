from decimal import Decimal
from types import SimpleNamespace

from app.config import Settings
from scripts import benchmark
from scripts.measurements import aggregate, markdown


def event(identifier, currency="RUB", cost="1", complete=True, **usage):
    return SimpleNamespace(
        id=identifier,
        currency=currency,
        cost=Decimal(cost) if cost is not None else None,
        cost_complete=complete,
        provider="yandex",
        model="actual-model",
        pricing_version="2026-10-04",
        **{
            field: usage.get(field)
            for field in (
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "tool_tokens",
                "embedding_tokens",
            )
        },
    )


def test_report_attempt_dedup_unknown_tokens_and_separate_currencies():
    first = event("one", input_tokens=100, output_tokens=20, cached_tokens=10)
    retry = event("two", cost=None, complete=False)
    third = event("three", currency="USD", cost="0.01", embedding_tokens=50)
    report = aggregate([first, first, retry, third])
    assert report["calls"] == 3
    assert report["tokens"]["input_tokens"] == {"known": 100, "unknown_calls": 2}
    assert report["tokens"]["tool_tokens"] == {"known": None, "unknown_calls": 3}
    assert report["tokens"]["embedding_tokens"] == {"known": 50, "unknown_calls": 2}
    assert report["costs"] == {
        "RUB": {"known": "1", "incomplete_calls": 1},
        "USD": {"known": "0.01", "incomplete_calls": 0},
    }
    rendered = markdown(
        {
            "measured_at": "today",
            "revision": "abc",
            "protocol": "native",
            "status": "failed",
            "scenarios": {"PDF": report},
        }
    )
    assert "неполная" in rendered and "USD" in rendered and "неизвестных" in rendered
    assert "actual-model" in rendered
    unknown = aggregate([event("unknown", cost=None, complete=False)])
    assert unknown["costs"]["RUB"] == {"known": None, "incomplete_calls": 1}


async def test_benchmark_missing_credentials_exits_before_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(benchmark, "settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        benchmark, "session_factory", lambda: (_ for _ in ()).throw(AssertionError("DB accessed"))
    )
    args = SimpleNamespace(output=tmp_path / "report", revision="test")
    assert await benchmark.main(args) == 2
    assert '"pending"' in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
