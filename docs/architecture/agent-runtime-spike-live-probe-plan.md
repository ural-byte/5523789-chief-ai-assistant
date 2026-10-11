# План продолжения live POC: capture-only gate и временный bot switch

PLAN_READY — только для ограниченного capture probe. Переключение бота пока NO_GO.

Planner: **TL;DR:** Самый ранний полезный шаг — отдельный temporary HTTPS MCP capture endpoint без БД, Telegram token и production credentials. Два overlapping Responses запроса проверяют request-scoped header binding; это не доказывает native Atelier identity и не разрешает включить backend tools. Native Atelier дополнительно проверяется одним минимальным UI/MCP вызовом, если generation заработает. В bigbang отсутствует Ingress; current backend/db — ClusterIP. До успешных transport/source/delivery gates SPIKE_BINDING_VERIFIED=false и текущий бот остаётся на существующем runtime. Root выполняет только конкретно подготовленное временное переключение и rollback после независимой проверки.

Подробный план

## Goal / состояние

Workspace: /Users/frux/.codex/worktrees/agent-runtime-spike/5523789-chief-ai-assistant; branch codex/agent-runtime-spike; HEAD ba657b9b36fadc8603cde7538e185345fdcbc3c1, base 4ccfe10bd1550844f010c4f2a7efea1e765b42e8. Existing adapter 51 PASS; full Checker FAIL live NOT_RUN, Reviewer NOT_RUN. Новый тикет URALBYTE-13 подтверждён; has-epic link 25 к URALBYTE-3 подтверждён. PR требует ссылки на этот verified тикет и завершённых gates.

Новая явная пользовательская авторизация допускает временно использовать активный @UralbyteDemoBot с прежним token. Она заменяет прежнее требование отдельного бота именно для этого прогона; isolated data, owner isolation, backend approvals и rollback сохраняются. Planner не читает .env/Secret и не переключает bot/polling.

A preferred. Native agent aac99aa41o220jk4pqbq: V5 YandexGPT5.1Pro+3Skills/no builtins genericFAIL; V6 same model/reasoningOFF/noSkills/no builtins/fresh context STREAM_TIMEOUT, request-id9cf76fbe-0b61-4abb-8d27-cb782d3dce9f. Причины UNKNOWN, непригодность A не доказана. Диагностическая серия завершена, новые model retries в capture scope не входят. B official Responses real function roundtrip PASS, 893+433ms, 83+145tokens по evidence Root; это weather capability probe, не Telegram acceptance.

## Repository observations

Existing /mcp, включая initialize/tools/list, требует binding gate. tools/call требует separate MCP credential и X-Spike-Event-Binding. /transport/updates с separate transport secret создаёт signed event binding; per-operation invocation id вычисляется из trusted event/tool/validated args. Existing transport receipts связывают actual sendMessage message_id/chat/approval с callbacks. Worker осуществляет scheduler/callback/delivery, но не ingress poller и не вызов Atelier. Поэтому existing kit сам не может обеспечить native event→MCP header.

Документированный Responses tools.mcp.headers даёт candidate request-scoped transport; McpGateway.forward_headers тоже существует. Ни одно поле не доказывает, что native Telegram publication создаёт доверенные user/chat/source metadata.

## Proposed architecture: отдельный probe, production gate не трогать

Hardworker ownership: app/spike_atelier/probe.py, scripts/spike_mcp_probe.py, tests/spike_atelier/test_probe.py, docs/architecture/agent-runtime-spike-live-probe-plan.md и probe evidence schemas. Existing API/service/config/worker, Runtime, production files и чужие изменения не менять для capture-only этапа.

Temporary отдельная Cloud Function + API Gateway HTTPS, без DB/VPC/backend/Telegram secrets. Root создаёт только label-scoped probe resources; никаких прав доступа к существующим Secret/DB и broad IAM. Защита fresh dedicated probe bearer, TLS, hard expiry <=30min, explicit cleanup. Функция не использует logging request body/headers, платформа access logs не должны содержать auth/text/body. IAM точный вызывающий API Gateway→Function; настройки authority не публикуются в docs.

### Endpoint contract

POST /mcp, JSON-RPC 2.0, Streamable HTTP stateless JSON subset; methods initialize, notifications/initialized, tools/list, tools/call. Только existing имя list_tasks; probe schema допускает единственный аргумент limit из enum [7,11], без cursor и extra fields. Synthetic output пустой список, никаких backend handlers. Это ограничение capture fixture, не изменение runtime schema. Иного LLM tool нет. Browser Origin reject, input <=16KiB, bounded timeout/calls, expiration HTTP410, bad auth401. Notifications initialized202; unsupported methods -32601; bad schema -32602.

Fresh probe signing key выпускает два signed synthetic bindings E1/E2 с expiration и nonce. До запросов Root отдельно фиксирует независимую expected correlation: request Q1 обязан вызвать list_tasks({"limit":7}), request Q2 — list_tasks({"limit":11}); canonical argument fingerprints F1/F2 рассчитываются из validated JSON и НЕ выводятся из headers/bindings. Private expected map Q1/F1→E1, Q2/F2→E2 хранится вне model context. Сам факт валидного header не доказывает правильную корреляцию. tools/call требует X-Spike-Event-Binding, missing/invalid/expired403. Binding отсутствует в arguments/results и model text. initialize/list auth protected, но не требуют event binding: сервисное discovery не даёт dataset authority.

Redacted capture использует только фиксированную allowlist полей: validated run UUID, timestamp/latency, method enum [initialize,notifications/initialized,tools/list,tools/call,unsupported], tool enum [list_tasks,unsupported], argument fingerprint/tag F1/Q1 или F2/Q2, verified binding hash/tag E1/E2 и verdict. Разрешённые JSON keypaths/types: jsonrpc, id, method, params, params.name, params.arguments, params.arguments.limit; только наличие/type, без arbitrary values. Header metadata — presence/type только фиксированных Authorization, X-Spike-Event-Binding, Content-Type, MCP-Protocol-Version, MCP-Session-Id, X-Request-Id, Traceparent; неизвестные имена/значения не сохранять. Никаких raw Authorization/binding/header values, model/user text, arguments, Telegram update body, raw RPC ID или credential-bearing URLs. Unsupported method/tool записываются константой unsupported, а не произвольной строкой клиента. Не отдавать модели headers/capture. Root собирает только такую проекцию structured evidence.

Для tools/call fingerprint определяется независимо от header. Matching F1/E1 и F2/E2 — candidate PASS только при known originating request Q1/Q2. Swapped F1/E2 либо F2/E1 — correlation FAIL, HTTP403; негативный тест должен подтвердить отказ без side effects. Missing fingerprint, непривязанный call, неизвестная связь с originating provider request или неоднозначность дают NOT_VERIFIED, никогда PASS по наличию двух разных валидных headers. Discovery initialize/tools/list не доказывает event correlation. Rate-limited/timeout/incomplete calls сохраняются отдельно; для параллельного PASS требуется actual overlap interval двух отличимых calls.

## Implementation plan

1. Hardworker реализует standalone probe contract/tests без touching existing binding gate. Challenger approves до code; Checker затем проверяет локальные criteria и secret redaction, Reviewer — probe/infra risks.
2. Root публикует exact reviewed probe на temporary function/gateway, сохраняет resource IDs/expiry/cleanup и TLS URL. Prod polling продолжает работать. Negative auth/missingbinding/expired probes до model requests.
3. Два overlapping server-created Responses requests Q1/Q2 используют отличимые synthetic tool requests limit7/limit11 и собственные tools.mcp.headers X-Spike-Event-Binding E1/E2, auth credential, allowed_tools list_tasks и platform MCP loop. Один response turn, без client function loop. Собрать provider response/request IDs, validated F1/F2 и actual capture correlation. Сопоставить headers с независимой expected map и originating request; отсутствие доказанной связи = NOT_VERIFIED. Обязательно swapped E1/E2 negative: correlation FAIL/HTTP403 для обоих mismatches. Только правильные positive mappings, доказанный overlap и rejects negatives допускают transport correlation PASS; после completion binding отзывается либо expires.
4. Native A: Root проверяет specific published invocation configuration — hidden dynamic header mapping source event→MCP call, а не static gateway header; signed source sender/chat/event provenance; callback routing/backend buttons/delivery receipt. Минимальный настоящий MCP list_tasks call UI при работающей generation, capture только field names/types. Один static call не PASS; synthetic UI header mapping не PASS native Telegram identity. Нет documented/verifiable authoritative source — A binding NOT_VERIFIED, full tools disabled.
5. Если B header roundtrip PASS, сохранить его как transport capability evidence. Возможный minimal bridge: trusted Telegram ingress создаёт binding для update, конкретный Responses invocation получает header серверно; все MCP calls этого invocation используют только его binding; previous_response_id сохраняется только для owner/chat history и не переносит прежние authority headers. Per-chat turns сериализуются, callbacks идут отдельным backend channel, с concurrent deliveries/ACK и event deadlines. Provider ведёт MCP multi-step, bridge не интерпретирует текст и не реализует tool loop. Этот live bridge требует отдельного concrete reviewed implementation handoff; текущий capture plan его не выдаёт за готовый.
6. После probe убрать temporary resources/credentials/capture с raw private expectations. Финальный отчёт раздельно содержит A generation, A native binding, B headers/tool loop, Telegram identity, delivery/callback и full acceptance verdicts. Непроверенные шаги NOT_RUN/UNKNOWN.

## Concrete bot switch / rollback gate

Root НЕ переключает бота ради capture probe. NO_GO пока выбранный A/B путь не имеет tested ingress/provider/tools/delivery/callback linkage, isolation, runnable hosting и approved switch package. Native A не публикуется на active bot при отсутствующей identity/без работающей generation.

В будущем package фиксирует current Deployment revision/replicas/image/commands, webhook state, durable polling checkpoint, active leased jobs и outbox; snapshots private и редактированные. Проверить startup isolated DB с _spike_test, single owner/private chat, отдельные fresh service credentials; live endpoint origin/auth/expiry; один polling/webhook owner.

Перед actual switch Root останавливает production polling в контролируемой maintenance boundary, ожидает прекращения active poll/leases, сохраняет backlog без drop_pending_updates. При невозможности остановить отдельный telegram worker допустим scale current assistant Deployment0: это останавливает также backend/background; PostgreSQL/PVC остаются. Не патчить token/Secret, не удалять webhook backlog/DB/PVC. Новый transport начинает с сохранённой границы и fresh provider context, реальные callbacks проверяются только для spike receipts/approvals.

Rollback при auth/binding error, двойном poll, loss delivery/callback, deadline или неработающей generation: остановить spike consumer, отключить его webhook/publication, убедиться в отсутствии второго consumer, восстановить previous webhook/replicas/commands и checkpoint по фактически подтверждённым events; никогда не откатывать offset ниже подтверждённого или выбирать offset с потерей pending events. Подтвердить readiness и работу прежнего runtime. Spike approvals не исполнять production callback handler; они остаются isolated и не перенесены в prod DB. Продолжительность temporary switch и end condition фиксирует Root до действия; автоматическое expiry endpoint не заменяет восстановление bot consumer.

## Acceptance / verification

- Probe noDB/noTelegramsecret/no production .env; narrow files only.
- Missing/wrong/expired auth-binding rejected; malformed/big requests reject; redaction tests с planted credentials/text.
- E1/E2 overlapping request mapping доказан независимыми argument fingerprints F1/F2 и originating Q1/Q2; swapped pairs correlation FAIL/403, unknown correlation NOT_VERIFIED. Нет mutable last-event/global current owner.
- Fixed capture allowlist: planted secrets в unknown method/tool/key/header name/value/RPC ID не попадают в evidence.
- Synthetic MCP PASS отдельно от genuine Telegram source/delivery/callback PASS.
- Existing SPIKE_BINDING_VERIFIED=false сохраняется до доказанного реального trust path.
- Ruff/focused probe tests; independent Checker и Reviewer.
- Actual full Telegram Sasha/Lesha chain и negatives остаются NOT_RUN до настоящего owner flow.

## Risks / non-goals

Responses headers могут не поддерживаться выбранной моделью/реальным gateway несмотря на schema; MCP subset может быть несовместим. Native Atelier generation/backend metadata сейчас не подтверждены. Нет existing public Ingress; temporary gateway cleanup обязательно. Capture-only endpoint не решает full backend hosting и не разрешает включить gate. Никаких seventh tools, model-provided source authority, backend regex/dialog workflow, Runtime LLM loop, full migration, production dataset mutations, PR до Tracker key.

## Primary sources

- https://aistudio.yandex.ru/ru/docs/ai-studio/api/Responses/createResponse — MCP headers schema (search source; full fetch слишком большой).
- https://aistudio.yandex.ru/ru/docs/ai-studio/operations/generation/mcp-server-access — provider MCP calls.
- https://yandex.cloud/en/docs/ai-studio/mcp-gateway/api-ref/grpc/McpGateway/update — forward_headers.
- https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/agents/text-agents — native publication/history; нет доказанного identity/callback guarantee.
