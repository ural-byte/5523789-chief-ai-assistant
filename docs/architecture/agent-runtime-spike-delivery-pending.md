# Delivery update architecture spike

Статус записи основного обновления: **POSTED**, URALBYTE-13 comment **101**.
Receipt: `action=comments.add`, `owner=URALBYTE-13`, `outcome=added`, `id=101`,
availability unavailable / ai_access_unavailable. Task **URALBYTE-13** и has-epic
связь с **URALBYTE-3** подтверждены receipts (link id25). Comment100 и comment101
не повторять. Ниже точный опубликованный текст; последняя локальная добавка — в конце.

Steward: Обновление architecture spike URALBYTE-13 существующего заказа URALBYTE-3 на 2026-10-11. Создание подтверждено receipt (creation key chief-agent-runtime-spike-2026-10-11), has-epic связь подтверждена receipt issueLinks.add id 25. Комментарий 100 в URALBYTE-12 не повторяется.

Канонический репозиторий: https://github.com/ural-byte/5523789-chief-ai-assistant . Изолированная ветка codex/agent-runtime-spike, base 4ccfe10. Согласованный план, adapter, независимый Checker, сравнение Planner по 13 критериям и промежуточный decision gate: docs/architecture/agent-runtime-spike.md, agent-runtime-spike-adapter.md, agent-runtime-spike-checker.md, agent-runtime-spike-comparison.md, agent-runtime-spike-decision.md. Sanitized evidence: docs/architecture/evidence/.

Реализован isolated runnable backend adapter с шестью реальными capabilities, trusted per-event binding, fail-closed внешним доступом, canonical delivery ACK и immutable keep/delete approval/callback. Независимый полный pytest до correction cycle 1: 578 PASS; после malformed callback fix: 51 spike PASS, 11.81 с, scoped Ruff/format/startup PASS. Локальных blocking defects не обнаружено. Последний полный Checker: FAIL: genuine Telegram / native binding / Skills и model behavior NOT_RUN. Reviewer: NOT_RUN; локальные PASS не заменяют живой gate.

Atelier V1/V2/V4 не дали успешного ответа, V5 YandexGPT с тремя Skills FAIL. Три actual Skills загружены/прикреплены в V3; V4 built-ins OFF. V6 YandexGPT 5.1 Pro без Skills/built-ins с fresh context завершилась FAIL preflight: stream timeout, request-id 9cf76fbe-0b61-4abb-8d27-cb782d3dce9f; успешного ответа нет. Screenshot сохранён локально data/spike-evidence/atelier-v6-preflight.jpg. Причина ошибок UNKNOWN, native MCP/Telegram binding не подтверждён.

Новое B evidence: faithful official-shape / default auto / YandexGPT без /latest завершил реальный synthetic function-call roundtrip: HTTP 200 за 893 мс, 83 tokens и final answer HTTP 200 за 433 мс, 145 tokens. Ранние timeouts сохраняются, их причина UNKNOWN. Stored context probe: первый HTTP 200 за 727 мс, 30 tokens, previous_response_id continuation ReadTimeout 45008 мс; input-history replay ReadTimeout 45380 мс. Successful context continuation и genuine Telegram acceptance не получены; стоимость UNKNOWN.

Пользователь отдельно явно разрешил временно переключить действующий @UralbyteDemoBot на spike с активным token, затем вернуть. Это исключение к первоначальному отдельному тестовому боту; временный switch на момент обновления не подтверждён. Секреты в комментарий/skills не передаются. Orchestrator возобновил планирование native binding probe. Разрешение на временный тест не является разрешением полной миграции.

Предварительный decision gate INCONCLUSIVE/NOT_DECIDED. Пригодность A/B, объективная непригодность обоих и основание предпочесть C не установлены. Следующие gates: trusted owner/chat/source hidden binding + canonical ACK / exact callback; успешный model/context/multi-step; точная Telegram-цепочка Саша/Лёша→конфликт→«Лёша — правильный вариант»→backend buttons→genuine owner confirm→fresh retrieval только Лёши; negative approvals, измерения; полный Checker PASS → Reviewer → Planner comparison → Steward recommendation → отдельное решение пользователя о миграции.

Создание/связь подтверждены при скрытом issue content; AI access check on. Чтение issue metadata ещё HTTP 429, фактический status / transition IDs недоступны, статус не угадывается. Задача не закрыта, full spike не объявлен complete; прежние URALBYTE-9/11/12 не закрыты, PR этим агентом не создавался. Активное время по ролям надёжно не измерено, worklog не выдуман.

Идентификатор записи: chief-agent-runtime-spike-continuation-2026-10-11.

## Локальное дополнение после публикации comment 101

В Tracker отдельно **NOT_POSTED**. [Normalized messages context probe](evidence/chief-responses-normalized-context-probe-evidence.json)
завершился ReadTimeout 45312 мс; причина UNKNOWN, retries модели остановлены.
[Sanitized V6 evidence](evidence/chief-atelier-v6-preflight-evidence.json) сохранён
для уже отражённого stream timeout. Decision остаётся INCONCLUSIVE/NOT_DECIDED.
