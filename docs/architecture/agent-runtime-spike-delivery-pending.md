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

## Draft PR7: обратная ссылка в Трекер

[Draft PR7](https://github.com/ural-byte/5523789-chief-ai-assistant/pull/7) создан
и attached Handyman; task URALBYTE-13 уже подтверждён. Backlink comment:
**NOT_POSTED** — comments.add вернул HTTP 429 без receipt. Whoami сначала тоже
HTTP 429, один bounded retry после45 секунд подтвердил qdinov/cloud org
bpf53toodvl9r97aasc2. AI access check ON. Последующие Tracker calls остановлены;
комментарии100/101 не повторяются. Ниже подготовленный текст обратной ссылки.

Steward: Для architecture spike URALBYTE-13 создан Draft/WIP PR: https://github.com/ural-byte/5523789-chief-ai-assistant/pull/7 . Обратная ссылка на задачу: https://tracker.yandex.ru/URALBYTE-13 .

Зафиксированное представление поставки: OPEN, isDraft=true; base main, head codex/agent-runtime-spike, snapshot c908f98c3332ce3cb84bebd53f10d1fe49daa3df. PR содержит изолированный backend adapter и согласованные документы/evidence. CI в этом обновлении не проверен, статус UNKNOWN.

Полный spike не завершён: INCONCLUSIVE / NOT_DECIDED. Последний полный Checker: FAIL по внешнему gate; genuine Telegram и native binding NOT_RUN; Reviewer: NOT_RUN. Локальные тесты и успешный синтетический Responses roundtrip не означают живой PASS. @UralbyteDemoBot на момент этого обновления NOT_SWITCHED; временный тест отдельно разрешён пользователем с возвратом исходного транспорта.

Возобновлено ограниченное планирование capture probe: round 2 Plan Challenger APPROVE. Hardworker реализует только probe/CLI/tests для проверки native binding; работа ещё не проверена. Это дополнительный исследовательский шаг, без полной миграции и без заявления PASS.

PR остаётся Draft/WIP до требуемых проверок. Merge, постоянное переключение production, закрытие задачи и выбор архитектуры этим обновлением не выполняются. Комментарии100/101 не повторяются.

Идентификатор обратной ссылки: chief-agent-runtime-spike-pr7-backlink-2026-10-11.

## Checkpoint для следующего delivery update

Финальные scoped counts получены; итоговая запись в Трекер **NOT_POSTED**.
Перед публикацией whoami вернул HTTP 429, активную организацию в этой попытке
подтвердить не удалось. В соответствии с ограничением root retry разрешён
только после auth success; dry-run/comments.add не выполнялись, Tracker calls
остановлены. Готовый итоговый текст: `/private/tmp/chief-spike-final-checkpoint-comment.md`. Обратная ссылка PR7 пока NOT_POSTED;
comments100/101 уже сохранены, не повторять.

- Draft PR7, exact head c908f98c3332ce3cb84bebd53f10d1fe49daa3df: обе CI checks
  SUCCESS; run38151798051 — 593 PASS, 7 warnings, 265.97 с.
  [Evidence](evidence/chief-spike-ci-c908f98-evidence.json).
- Configured IAM HTTP 200; первая read-only Cloud Functions runtimes проверка
  HTTP 503, 1242 мс общая latency. Повтор IAM HTTP 200 → GET ReadTimeout12 с,
  общая latency13046 мс. [First evidence](evidence/chief-cloud-runtime-read-evidence.json),
  [repeat evidence](evidence/chief-cloud-runtime-read-repeat-evidence.json).
- Ресурсы/endpoint не созданы; actual MCP capture и live Telegram NOT_RUN;
  бот NOT_SWITCHED. Capture code завершён: независимый scoped Checker
  **87 PASS**, 12.43 с (51 прежний + 36 новых), scoped Ruff/format PASS,
  standalone ZIP unauthenticated request 401 PASS; новых локальных defects нет.
  Reviewer NOT_RUN. Эти counts не выдаются за CI нового remote head: Handyman
  готовит update PR7, будущий SHA и его CI ещё не известны.
- Decision INCONCLUSIVE/NOT_DECIDED; full migration/merge/closure не выполнены.

[Передача следующего шага](agent-runtime-spike-decision.md) определяет необходимые
проверки, checkpoint и единственную будущую delivery-запись после финальных counts.
