# Инженерный план architecture spike

PLAN_READY

Planner: **TL;DR:** Предлагаю изолированный spike adapter к существующим backend handlers без изменений production Runtime. Сначала проверяем реальный контракт Atelier: доверенную identity, связь с исходным Telegram событием, доставку карточки и callback. До подтверждения этих свойств внешний доступ tools отключён; независимую серверную часть можно реализовать и проверить локально. Продуктовых уточнений нет; отсутствие отдельного тестового бота означает NOT_RUN обязательной живой приёмки. После независимого challenge реализацию передать Hardworker, а выбор архитектуры оставить пользователю.

Подробный план

## Goal

Проверить Telegram → Agent Atelier → tools → existing backend, сохранив owner isolation, идемпотентность и исполнение destructive actions исключительно через backend approval.

- Repository: ural-byte/5523789-chief-ai-assistant.
- Workspace: /Users/frux/.codex/worktrees/agent-runtime-spike/5523789-chief-ai-assistant.
- Branch: codex/agent-runtime-spike.
- Exact base: 4ccfe10bd1550844f010c4f2a7efea1e765b42e8.
- Заказ: URALBYTE-3; приоритет также сохранён комментарием 100 URALBYTE-12.
- Новый тикет: TASK_KEY_PENDING. До verified key PR запрещён.
- Product source: [согласованный план](agent-runtime-spike.md).

## Repository observations

1. SaveMemory, SearchMemory, CreateTask, PrepareMemoryResolution работают с реальной PostgreSQL без Runtime.
2. SearchMemory.apply сохраняет MemoryContext, точные snapshots и shown_pairs. PrepareMemoryResolution принимает conflict_ref/retain_ref; backend вычисляет конкретные keep/delete.
3. Подготовка удаления требует подтверждённой доставки конфликта через bind_shown_delivery, Outbox и ACK. Получение MCP результата моделью не заменяет эту доставку.
4. Approval/ApprovalPreview immutable; existing handle_callback проверяет owner/chat, TTL, hash, snapshots, cancel и replay.
5. ToolContext содержит доверенные owner/chat/source/timezone/idempotency. Эти поля нельзя принимать из model arguments.
6. Registry не поддерживает list_memory/list_tasks; memory_page связан с Telegram rendering. Достаточно двух небольших owner-scoped paged readers existing tables.
7. Existing API ingress содержит conversational routing и agent jobs; его целиком не подключать к spike.
8. Tests используют disposable PostgreSQL/pgvector и запрещают очистку БД без суффикса _test.

### Atelier preflight, 2026-10-11

Root подтвердил private agent chief-runtime-spike-20261011, ID aac99aa41o220jk4pqbq. V1 сохранена: DeepSeek4Flash, reasoning OFF, max output 512, temperature 0.3. Доступны отдельные Add MCP servers, Skills и instructions. Tools не подключены, агент не опубликован. Два synthetic UI запроса закончились ошибкой «Не удалось получить ответ агента» без ответа; model preflight не пройден.

MCP Add/Create предлагает External MCP, HTTP/HTTPS API, Cloud Functions и Workflows. External form: HTTP with SSE/Streamable HTTP, private/public (default private), optional service account/network/logs. MCP не создан. На просмотренном первом шаге не видны trusted Telegram owner/chat/event, callback, delivery ACK или allowlist. Это UNKNOWN/NOT_VERIFIED, а не доказательство отсутствия поддержки: следующие поля/API ещё не обследованы.

Документация описывает [MCP tools](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/agents/tools/mcp-tool) и [каналы агентов](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/agents/text-agents), но требуемый контракт пользовательской авторизации должен быть проверен реальным probe.

## Product decisions applied

- Отдельные агент, тестовый бот, owner/data.
- Только шесть capabilities и три instructions/skills.
- Backend сохраняет authority для данных, permissions, audit, idempotency и approvals.
- delete_memory только готовит approval; confirm/cancel отсутствуют среди LLM tools.
- Нельзя добавлять backend regex follow-up, conversational state machine или agent loop ради PASS.
- Genuine Telegram chain обязательна; mocks/API-only evidence учитываются отдельно.
- A приоритетен при доказанном PASS; B проверяется при конкретных ограничениях A.
- Миграция и production deployment требуют отдельного решения пользователя.

## Proposed architecture

Изолированный app/spike_atelier/ с отдельными startup и registry, scripts, tests и docs. Production composition root его не импортирует.

### Provisional trusted contract

- MCP credential доказывает сервис, а не owner. Owner берётся из проверяемого Telegram события или подтверждённого платформой hidden context.
- Согласованная single-owner credential binding допустима для isolated test data только после доказанного запрета доступа посторонних к каналу. Это не multi-owner support.
- Backend создаёт Update/Operation/Invocation/ToolContext. Source text, message timestamp, owner/chat, timezone и idempotency не входят в model arguments.
- Tool call серверно связан с конкретным trusted source event. Нельзя использовать глобальный last request/mutable active-owner.
- Stable event binding и server invocation identity обеспечивают idempotency; replay возвращает existing result. Повтор выбора того же conflict сохраняет existing approval/keep/delete.
- Без verified binding внешний endpoint отклоняет вызов до provider/DB side effects.
- Backend публикует canonical conflict/approval cards через existing Outbox. shown authority возникает только после genuine successful Telegram ACK; model ACK недостаточен.
- Callback channel получает trusted a:<approval_id>:y/n, создаёт leased callback job и вызывает existing handle_callback без LLM.
- Подмена owner/source/call_id/approved=true/delivery status через tools запрещена; model-provided authority не принимается.

### Шесть schemas

- save_memory: existing SaveArgs.
- search_memory: existing SearchArgs; provenance, обе версии, shown_pairs.
- list_memory: bounded pagination owner records, cursor ownership validation.
- delete_memory: existing ShownResolutionArgs. conflict_ref/retain_ref из shown_pairs определяют точные immutable keep_entry_id/delete_entry_id. Только PrepareMemoryResolution.
- create_task: text и existing DateSpec; resolve_date относительно trusted message/reference_at/timezone. complete_request не экспортировать как conversational control.
- list_tasks: bounded pagination active owner tasks.

## Implementation plan

1. **Early gate.** Root проверяет реальные model/version, MCP transport/auth, hidden metadata, Telegram access control, buttons, callback, delivery ACK минимальным probe с synthetic data. Внешние mutating tools отключены. Сохранить redacted evidence и отдельные verdicts.
2. **Independent adapter kit.** Separate settings/startup без production .env; explicit disposable DB/test config. Contracts, trusted event binding, invocation/result persistence. Непроверенный live binding отключён.
3. **Capabilities/delivery.** Четыре existing handlers и два paged readers. Owner locking, context epoch, snapshots, idempotency, canonical Outbox, bind_shown_delivery и ACK. Runtime/continue_resolution_turn не использовать для поведения агента.
4. **Callback path.** Thin transport adapter к existing callback jobs/handler. Telegram credentials только транспорту; MCP/Skills не получают execution capability. Missing/failed delivery не создаёт shown authority.
5. **Instructions/tests.** Versioned memory-management/task-management/destructive-actions. System instructions и реально доступный Skills mechanism фиксировать отдельно. Meaningful adapter/security tests.
6. **Live после gate.** Только при proven identity/binding подключить tools к private Atelier agent и test bot; exact live chain и negatives. При ограниченном native channel допускается minimal ingress/callback transport без conversational routing. UNKNOWN contract остаётся blocker; без бота live NOT_RUN.
7. **Evidence/report.** Redacted traces, sample size, usage/latency/cost, все 13 A/B/C criteria. При конкретном ограничении A минимальный real Responses capability probe: tool request и function_call_output при необходимости. Без новой полной orchestration; probe не объявлять B Telegram PASS. При нехватке данных INCONCLUSIVE/NOT_DECIDED.

### Ownership

Hardworker: app/spike_atelier/**, отдельные spike scripts/tests и docs/architecture/**; при необходимости отдельный spike dependency manifest. Production files, migrations, deployment, existing dirty changes и старые worktrees сохраняются. Исполнитель не один в codebase; чужие изменения не отменять.

## Risks

- Native event binding может быть непроверяемым; одного MCP credential недостаточно.
- Native Telegram может не поддержать backend buttons/callback/proven delivery.
- У test bot один transport owner; два long-poll workers создают гонку.
- Telegram retry boundary at-least-once; DB side effects идемпотентны.
- External history/Skills retention/export пока UNKNOWN.
- Без test bot обязательный live gate не завершён; без verified Tracker key нет PR.

## Acceptance criteria

| Критерий | Реализация и доказательство |
| --- | --- |
| Six capabilities используют real data | Schemas, handler traces, PostgreSQL assertions |
| Нельзя подменить owner/source/idempotency | Real gate и negative auth tests |
| Follow-up выбирает Лёшу без нового уточнения | Separate live Telegram messages/agent trace |
| Choice сохраняет keep/delete | Immutable approval; обе записи до confirm |
| Genuine approval_id/buttons | Backend preview, actual delivery ACK |
| Confirm удаляет только Сашу без LLM | Trusted callback, audit, DB before/after |
| Нет возврата к выбору после choice_recorded | Simple confirm сохраняет same pending approval |
| Replay/cancel/expired/foreign/stale безопасны | Integration tests и доступные live checks |
| После очистки диалогового контекста конфликт исчезает | Fresh retrieval; Лёша/unrelated records сохранены |
| Сравнение честно | Все 13 criteria, UNKNOWN/NOT_RUN для пробелов |

Checker: PASS всей приёмки нельзя подменять PASS локальных автотестов при непройденном обязательном live gate.

## Verification plan

- Unit: strict schemas, unsupported tools, forbidden authority fields, cursor ownership, trace redaction, fail-closed prerequisites.
- PostgreSQL integration: real handlers, retries/concurrent duplicate choice; delete_memory ничего не удаляет.
- Delivery: missing/failed ACK блокирует authority; successful delivery связывает exact canonical conflict.
- Callback: existing handler path, cancel/replay/foreign owner-chat/TTL/stale snapshots, immutable payload-preview, unrelated data сохранены.
- Generation provider падает на любом LLM вызове: доказать отсутствие LLM на prepare/callback paths.
- Live checklist exact product chain с separate PASS/FAIL/NOT_RUN каждого шага.
- Ruff, focused spike tests, existing memory resolution/action/privacy tests; затем full suite.
- Independent Checker и Reviewer; отсутствие production/cluster changes.

## Explicit non-goals / out of scope

Полная миграция, production deployment, новый conversational workflow, расширение current Runtime, Gmail/Calendar/web search, дополнительные destructive tools, multi-owner certification, имитация Telegram acceptance и автоматический выбор архитектуры после spike.
