# Независимая проверка architecture spike

Checker: **FAIL** для полного spike. Local adapter behavior проверено;
blocking code defects не обнаружены. Внешние live prerequisites не выполнены.

## Проверки

- Независимый полный pytest до correction cycle 1: **578 PASS**, 204.89 s.
- После correction cycle 1: весь current spike suite **51 PASS**, 11.81 s.
- Scoped Ruff и format check: PASS.
- Реальный isolated uvicorn startup: PASS; default binding gate возвращает
  503 binding_not_verified.

Шесть real PostgreSQL capabilities, immutable keep/delete, mandatory canonical
ACK, exact approval_id/message_id callback, plain confirm, повтор выбора,
owner/chat isolation, replay, cancel, TTL, stale snapshots и fencing проходят.
После callback удаляется только Саша; Лёша и unrelated запись сохраняются.
Новый conversational regex/generation call в adapter отсутствует.

Найденная robustness ошибка malformed callback.message (HTTP 500) исправлена
проверкой типа до nested get. Независимый reproduction теперь 403 event_rejected;
15 negative shapes не создают DB/provider side effects.

## Непройденные обязательные gates

1. Genuine Telegram: конфликт → «Лёша — правильный вариант» → backend buttons →
   owner callback → fresh retrieval только Лёши: **NOT_RUN**.
   Нет отдельного бота и verified Atelier binding. Mocked transport и явно
   переданные structured refs не доказывают agent understanding.
2. Skills upload/attachment выполнены, но применение правил моделью,
   conversational context и multi-step loop: **NOT_RUN**.
   Частичный B function_call и последующий timeout не доказывают loop.

Рекомендация **NOT_DECIDED**, результат **INCONCLUSIVE**.
Общий Checker PASS не выдан, Reviewer **NOT_RUN** согласно freelance gating.
Production runtime/deployment не менялись. Для продолжения нужны genuine
prerequisites и полный live evidence; локальные тесты их не заменяют.

## Независимая проверка capture-only scope, 2026-10-11

Checker: **PASS только для локального bounded capture probe**. Полный spike —
**FAIL** из-за непройденных внешних live gates; Reviewer полного spike — **NOT_RUN**.

Проверенный snapshot: HEAD `c908f98c3332ce3cb84bebd53f10d1fe49daa3df` плюс четыре
новых capture-файла: `app/spike_atelier/probe.py`, `scripts/spike_mcp_probe.py`,
`tests/spike_atelier/test_probe.py`,
`docs/architecture/agent-runtime-spike-capture-run.md`. Критерии взяты из
согласованного round 2 плана `agent-runtime-spike-live-probe-plan.md`.

### Критерии и evidence

- Signed E1/E2 и независимые fingerprints limit7/11 проверены. Swapped пары
  отклоняются с HTTP 403 и verdict FAIL. Неизвестная originating correlation
  сохраняет NOT_VERIFIED; header сам по себе не даёт PASS.
- Evidence использует фиксированную allowlist. Неизвестные keys/header names,
  arbitrary method/tool values, raw RPC ID, credentials и model/user text
  исключаются. Provider metadata проецируется без копирования tools.headers.
- Expiry ограничен 30 минутами, body — 16 KiB, processing delay — 500 ms.
  Runner имеет конечный request budget, timeout и запрет redirects.
- Probe не читает DB, production .env или Telegram token. Нет mutable global
  owner/capture buffer. ZIP содержит только probe.py и fresh private fixture.
- Private files имеют права 0600, каталог — 0700; credentials не передаются
  через argv и exception text не выводится.
- Отдельный standalone ZIP smoke импортировал entrypoint без backend и проверил
  запрос без auth: HTTP 401 unauthorized. Временные файлы smoke удалены.

### Независимые команды

В подготовленном managed workspace с отдельной disposable spike БД:

- `python -m pytest tests/spike_atelier -q`: **87 PASS**, 12.43 s.
- Scoped `ruff check --no-cache`: **PASS**.
- Scoped `ruff format --check`: **PASS**.
- Standalone ZIP entrypoint smoke: **PASS**.

Полный pytest не повторялся для этого изолированного изменения; исторические
независимые результаты adapter выше сохраняются. Новых дефектов не обнаружено.

### Внешние ограничения

Actual provider-to-MCP roundtrip, native Atelier identity/binding и genuine
Telegram approval chain остаются NOT_RUN / NOT_VERIFIED. Локальная fixture не
доказывает эти свойства и не разрешает переключение бота или открытие backend
binding gate. Global ingress/request quota остаётся инфраструктурным prerequisite:
stateless handler сам его не обеспечивает, что явно указано в hosting handoff.

Checker не выполнял external API calls, deployment, Tracker или Git mutations
и не изменял implementation code. Общий Checker PASS не выдан.
