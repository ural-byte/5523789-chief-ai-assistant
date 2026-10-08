"""Aggregate billable HTTP attempts without inventing missing usage or mixing currencies."""

from decimal import Decimal

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "tool_tokens", "embedding_tokens")


def aggregate(events):
    events = list({str(event.id): event for event in events}.values())
    currencies = sorted({event.currency for event in events})
    return {
        "calls": len(events),
        "models": sorted({event.model for event in events}),
        "providers": sorted({event.provider for event in events}),
        "tokens": {
            field: {
                "known": (
                    sum(getattr(e, field) for e in events if getattr(e, field) is not None)
                    if any(getattr(e, field) is not None for e in events)
                    else None
                ),
                "unknown_calls": sum(getattr(e, field) is None for e in events),
            }
            for field in TOKEN_FIELDS
        },
        "costs": {
            currency: {
                "known": str(
                    sum(
                        (e.cost for e in events if e.currency == currency and e.cost is not None),
                        Decimal(0),
                    )
                )
                if any(e.currency == currency and e.cost is not None for e in events)
                else None,
                "incomplete_calls": sum(
                    not e.cost_complete for e in events if e.currency == currency
                ),
            }
            for currency in currencies
        },
        "pricing_versions": sorted({e.pricing_version for e in events}),
    }


def event_record(event):
    return {
        "id": str(event.id),
        "operation_id": str(event.operation_id),
        "logical_call_id": str(event.logical_call_id),
        "attempt": event.attempt,
        "provider": event.provider,
        "model": event.model,
        "operation_type": event.operation_type,
        "scenario": event.scenario,
        "status": event.status,
        "error_code": event.error_code,
        "started_at": event.started_at.isoformat(),
        "latency_ms": event.latency_ms,
        **{field: getattr(event, field) for field in TOKEN_FIELDS},
        "extra_usage": event.extra_usage,
        "cost": str(event.cost) if event.cost is not None else None,
        "cost_complete": event.cost_complete,
        "currency": event.currency,
        "pricing_version": event.pricing_version,
        "pricing_snapshot": event.pricing_snapshot,
    }


def markdown(report):
    lines = [
        f"Измерено: {report['measured_at']}; revision: {report['revision']}; "
        f"protocol: {report['protocol']}; status: {report['status']}.",
        "",
        "| Сценарий | Модели | Input / output / embedding tokens | Стоимость |",
        "|---|---|---|---|",
    ]
    for name, row in report["scenarios"].items():
        tokens = []
        for field in ("input_tokens", "output_tokens", "embedding_tokens"):
            value = row["tokens"][field]
            tokens.append(str(value["known"]) if value["known"] is not None else "неизвестно")
            if value["unknown_calls"]:
                tokens[-1] += f" (+{value['unknown_calls']} неизвестных вызовов)"
        cost = (
            "; ".join(
                f"{v['known'] if v['known'] is not None else 'неизвестно'} {c}"
                + (" (неполная)" if v["incomplete_calls"] else "")
                for c, v in row["costs"].items()
            )
            or "неизвестно"
        )
        lines.append(
            f"| {name} | {', '.join(row['models']) or '—'} | {' / '.join(tokens)} | {cost} |"
        )
    lines.extend(
        [
            "",
            "Стоимость — оценка по сохранённым ставкам, не счёт провайдера. "
            "PDF-индексация и вопрос разделены. Память включает сохранение и поиск. "
            "Неполные показатели не являются нулевым потреблением.",
        ]
    )
    snapshots = {}
    for event in report.get("events", []):
        snapshot = event["pricing_snapshot"]
        key = (event["model"], event["pricing_version"])
        snapshots[key] = (snapshot["currency"], snapshot["unit"], snapshot.get("rates"))
    for (model, version), (currency, unit, rates) in snapshots.items():
        lines.append(f"Ставки {version}, {model}: {rates}; {currency} за {unit} токенов.")
    return "\n".join(lines) + "\n"
