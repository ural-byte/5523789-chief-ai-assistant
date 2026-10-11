# Изолированный adapter Agent Atelier

Состояние на 2026-10-11: backend adapter реализован, внешний доступ по умолчанию
закрыт. Native Atelier binding и живой Telegram gate — **NOT_VERIFIED / NOT_RUN**.
Это runnable серверный POC, а не завершённая целевая интеграция или миграция.
Production bootstrap, Runtime, migrations и deployment его не используют.

## Контракт

`POST /mcp` реализует stateless JSON Streamable HTTP subset: initialize,
notifications/initialized, tools/list, tools/call. Protocol version 2025-03-26.
SSE, sessions, resources и sampling не реализованы; совместимость с Atelier
пока не проверена. Browser Origin отклоняется; поддерживаются только server-to-server
clients. Это ограниченный JSON subset, не заявление о полной MCP conformance.
Экспортируются ровно шесть tools из согласованного плана.
[Требования транспорта MCP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
проверены 2026-10-11.

Для tools/call нужны два независимых заголовка:

- Authorization: Bearer <MCP service credential>.
- X-Spike-Event-Binding: <подписанная backend привязка конкретного события>.

Привязку выдаёт только `POST /transport/updates` после проверки отдельного
X-Telegram-Bot-Api-Secret-Token, owner, private chat и структуры события.
Trusted bridge должен доставлять настоящий Telegram update и удерживать binding
в hidden metadata конкретного agent invocation. Binding не входит в schemas,
model arguments, tool results или обычные логи. Нет глобального current owner
или last event. MCP credential не даёт права создавать исходные события.

**Этот bridge ещё не подключён к Atelier.** Его наличие и передачу per-event
hidden metadata надо доказать отдельным probe. Native publication UI с bot token
и allowlist не подтверждает поддержку этого контракта. Нельзя вручную считать
binding проверенным лишь для включения tools. Для локальных тестов используется
явно помеченная fixture, которая не является live evidence.

Stable invocation UUID вычисляется сервером из operation, tool name и точных
validated arguments. Повтор возвращает сохранённый результат; owner lock
сериализует side effects. Исходное событие, время, timezone, epoch и idempotency
не контролируются аргументами модели. Event execution window — existing 90 секунд;
дальнейший follow-up получает новую привязку. Чужие, retired и expired события
отклоняются до provider/DB side effects.

## Память и approval

SaveMemory, SearchMemory, CreateTask и PrepareMemoryResolution переиспользуются
напрямую. list_memory/list_tasks — два bounded owner-scoped readers с проверкой
владельца cursor. create_task использует DateSpec и trusted source timestamp;
complete_request не экспортируется. Existing scheduler исполняет напоминания
отдельного owner в transport worker.

Search сохраняет реальные snapshots/shown_pairs и ставит каноническую карточку
с a/b в Outbox. Только successful Telegram API receipt с message_id и нужным chat
даёт ACK; MCP результат и model ACK не считаются доставкой. Existing
bind_shown_delivery/shown_authority проверяют точный текст и context epoch.

delete_memory принимает только conflict_ref/retain_ref. Backend сохраняет
immutable choice_recorded, keep/delete, approval_id и preview. Обе записи остаются
до callback. Повтор выбора, в том числе противоположный retain_ref того же conflict,
возвращает существующий pending approval. Никаких правил распознавания «Лёша»,
«да», «удалить» или «a» в adapter нет: понимание follow-up принадлежит агенту.

Отдельная spike_telegram_receipts сохраняет только transport receipt IDs,
owner/chat/message_id и approval_id, без текста или credentials. Это позволяет
сопоставить callback с реально доставленной approval карточкой и сохранить replay
после scrub исходных копий. Таблица создаётся только в isolated spike БД;
production migration не добавлена.

Callback a:<approval_id>:y/n принимается только trusted transport, с owner/chat и
message_id соответствующего receipt. Создаётся leased callback Job, который
вызывает existing handle_callback. Execution, TTL/hash/snapshots, cancel и audit
остаются в existing engine. confirm/execute/delete SQL capabilities среди tools
нет. Worker не вызывает generation provider и не интерпретирует пользовательский
текст. Простая текстовая фраза не создаёт callback job.

## Запуск

Используются существующие project dependencies. Создай отдельную PostgreSQL БД
с pgvector и суффиксом `_spike_test`; production URL отвергается. Передай настройки
через окружение `SPIKE_`, без чтения `.env` рабочего приложения:

| Переменная | Назначение |
| --- | --- |
| SPIKE_DATABASE_URL | URL отдельной disposable БД |
| SPIKE_OWNER_ID / SPIKE_CHAT_ID | Изолированный owner и private chat |
| SPIKE_TIMEZONE | По умолчанию Asia/Yekaterinburg |
| SPIKE_MCP_SECRET | Отдельный service credential, минимум 32 символа |
| SPIKE_TRANSPORT_SECRET | Отдельный webhook/bridge secret, минимум 32 символа |
| SPIKE_BINDING_SECRET | Отдельный ключ backend event binding, минимум 32 символа |
| SPIKE_BINDING_VERIFIED | false по умолчанию, до документированной проверки |
| SPIKE_BINDING_EVIDENCE | Ссылка на доказательство контракта; нужна для true |
| SPIKE_AI_API_KEY / SPIKE_AI_FOLDER_ID | Embeddings provider только backend |
| SPIKE_TELEGRAM_BOT_TOKEN | Bot token только transport worker; для текущего прогона пользователь разрешил временный @UralbyteDemoBot после switch/rollback gates |

Секреты должны различаться. После настройки:

```sh
python -m scripts.spike_atelier_db
python -m app.spike_atelier
python -m app.spike_atelier.worker
```

API слушает только 127.0.0.1:8011; внешний TLS/bridge и webhook настройка не
создаются автоматически. MCP/transport credentials не включать в skills и
model context. HTTP access logs отключены; worker не пишет Telegram URL с токеном.
`spike.tools` пишет только name/invocation/status/latency_ms. Persisted Invocation
даёт tool trace, AICall — existing embeddings usage; generation usage Atelier
требует отдельного экспорта и пока UNKNOWN.

Для остановки заверши два процесса. Сам adapter не выполняет destructive teardown,
не переключает production webhook и не запускает long polling. Disposable БД
удаляется отдельно только после сохранения нужного evidence.

Три versioned behavioral sources: [memory-management](../../app/spike_atelier/skills/memory-management.md),
[task-management](../../app/spike_atelier/skills/task-management.md),
[destructive-actions](../../app/spike_atelier/skills/destructive-actions.md).
Для платформы с обязательным именем SKILL.md упакуй каждый source отдельно под
этим именем, без изменения текста. Upload не доказывает фактическое применение.

## Проверка

```sh
TEST_DATABASE_URL=<disposable PostgreSQL URL ending _test> python -m pytest tests/spike_atelier -q
ruff check app/spike_atelier scripts/spike_atelier_db.py tests/spike_atelier
```

Тесты используют настоящую PostgreSQL, fixture embeddings и mocked Telegram
transport. Они доказывают domain/security behavior adapter, а не качество
Atelier follow-up, настоящий owner click или platform identity. Exact adapter
chain включает конфликт, structured choice, delivery, exact callback, повтор,
новый retrieval и сохранение unrelated записи. Отдельно проверяются plain confirm,
параллельный replay, missing/failed/wrong ACK, foreign owner/chat/cursor, TTL,
stale snapshots, immutable payload/preview, source fencing и redacted errors.

Полный live gate и сравнение A/B/C остаются в [плане](agent-runtime-spike.md).
Рекомендация — **NOT_DECIDED**, итог — **INCONCLUSIVE** до genuine evidence.

Результаты implementation checks, 2026-10-11:

- Existing baseline + spike full suite: 574 PASS, 188.96 секунд.
- После финальных Origin/retry/startup/scheduler additions: весь текущий isolated
  spike suite — 36 PASS, 10.53 секунд. Эти additions не меняют production modules.
- Scoped Ruff и format check — PASS.
- Реальный isolated schema/startup smoke — PASS; без verified binding tools/list
  возвращает 503, receipt schema создаётся только в spike БД.
- Полный новый diff проверен; production files/migrations/deployment не изменены.

Это проверки исполнителя. Независимые Checker/Reviewer gates и обязательная
Telegram приёмка ими не заменяются. Внешний endpoint не публиковался.


## Фактический platform preflight

Создан private agent `chief-runtime-spike-20261011`,
ID `aac99aa41o220jk4pqbq`, folder `b1go0lvlaf559s8t1qhi`.
V1: DeepSeek 4 Flash, reasoning off, max output 512, temperature 0.3.
Синтетический запрос «В этом тестовом диалоге кодовое слово — Кедр.
Ответь только этим словом.» завершился stream timeout
(request-id `1cc8d086-f527-4881-9f3d-1922ecd08135`); один повтор дал
«Не удалось получить ответ агента». Контрольная V2 с Alice AI LLM Flash
и теми же настройками тоже не дала ответа. Успешная model/context проверка
не заявляется. UI screenshot сохранён локально в исходном checkout:
`data/spike-evidence/atelier-v1-preflight.jpg`.

В native Telegram publication form проверены только token и видимость
public/restricted allowlist. Форма закрыта без токена и публикации.
Наличие trusted owner/chat/update metadata, routing callback и delivery ACK
остаётся UNKNOWN; отсутствие этих полей на одном экране не доказывает
невозможность платформы.

UI Skills предлагает SKILL.md/ZIP до 50 MB. Extension file chooser отказал
из-за запрета «Allow access to file URLs»; разрешение не менялось.
Загрузка выполнена через штатный macOS picker с выбором каждого конкретного
SKILL.md. Все три навыка созданы, прикреплены и сохранены в V3.
Затем в V4 выключены обнаруженные built-in defaults Image Generation,
Web Search и кодовый интерпретатор (в V1–V3 были включены по умолчанию).
V4: Alice AI LLM Flash, три навыка, no MCP/publication. Контрольный model probe
после отключения defaults тоже дал «Не удалось получить ответ агента».
Upload/attachment PASS; фактическое применение skills, context и loop NOT_RUN.
Итоговый local screenshot: `data/spike-evidence/atelier-v4-skills.jpg`
в исходном checkout. Ни credentials, ни backend authority в skills не переданы.

Реальный синтетический B probe отправлялся на официальный
`POST https://ai.api.cloud.yandex.net/v1/responses`, max output 256, store false,
forced function `probe_echo(text)`, parallel tool calls false; использованы
только тестовое слово и функция echo, без backend действий или owner data.
Второй запрос включал исходное user message, полученный function_call
и соответствующий function_call_output; tool_choice none.

- DeepSeek V4.1 Flash: первый запрос ReadTimeout, 45424 ms, без response/usage.
- YandexGPT latest: первый запрос HTTP 200, 791 ms, реальный function_call
  `probe_echo({"text":"Кедр"})`, 82 input + 13 output = 95 tokens.
  Второй запрос с результатом функции ReadTimeout, 45014 ms.
- Цена, complete roundtrip, conversational quality и живой Telegram B —
  UNKNOWN / NOT_RUN. Это частичный протокольный результат, а не пригодность B.

Sanitized evidence: [DeepSeek](evidence/chief-responses-probe-evidence.json),
[YandexGPT](evidence/chief-responses-yandexgpt-probe-evidence.json).
Тайм-ауты не устанавливают причину ошибки и не доказывают архитектурную
непригодность A/B. Итоговое решение остаётся NOT_DECIDED / INCONCLUSIVE.

Tracker: согласованный приоритет записан Steward в URALBYTE-12 comment 100
(longId `6acb645920b4f92791265df0`, 2026-10-11T10:26:33Z).
Создание отдельного тикета с creation key
`chief-agent-runtime-spike-2026-10-11` вернуло HTTP 429 без ключа.
Последующая проверка whoami тоже HTTP 429; доступ не отключался.
PR отсутствует до получения конкретного Tracker task key.


### Skills version manifest

| Skill | Atelier ID | Uploaded SHA256 |
| --- | --- | --- |
| memory-management | skill_838e6270e072474e9a1d340a27ab7ec5 | `f449d7821720c3d071e32b4d3c38e6340a6e4385a41e48fa78befdecbd987050` |
| task-management | skill_a34ce124e9a74220ab746c260bd1b18d | `554d04724c12bf943e17679f0614ed4e23519f2203d6a3fb210f5b31fa98ab9c` |
| destructive-actions | skill_92dd9945260041d6943066fa804b084d | `da921dcc219be0b465ef2e48114cbe1a3f616cd15be927e8ce9bca55f2fe13a6` |


Дополнительный B probe через `previous_response_id` со store true:
[YandexGPT context evidence](evidence/chief-responses-yandexgpt-context-probe-evidence.json).
Первый запрос HTTP 200/function_call, 857 ms, 95 tokens; второй запрос только
function_call_output и previous_response_id завершился ReadTimeout, 45004 ms.
Полный loop и context suitability по-прежнему не подтверждены.

Steward после четырёх часов повторно проверил whoami успешно (тот же account/org),
но epic metadata и idempotent issue create снова HTTP 429. Новый комментарий
с результатами в URALBYTE-12 дважды HTTP 429, включая retry после 45 s; он
**NOT_POSTED**. Ранее опубликованный plan comment 100 не повторялся.
[Подготовленное delivery update](agent-runtime-spike-delivery-pending.md)
не является долговременной записью в Tracker или свидетельством поставки.


## Новое evidence и авторизация, 2026-10-11

Предыдущие записи выше сохраняют историю probes; текущий результат B обновлён:

- [Official Responses roundtrip](evidence/chief-responses-official-probe-evidence.json): HTTP200 function_call893ms,83tokens (70input/13output); HTTP200 function_call_output continuation433ms,145tokens (132input/13output,cached32), final weather answer. Function-call protocol PASS; platform MCP loop и Telegram Sasha/Lesha acceptance NOT_RUN.
- [Stored context](evidence/chief-responses-context-official-probe-evidence.json): firstHTTP200727ms,30tokens (28input/2output); secondReadTimeout45008ms, result/usage UNKNOWN.
- [Replayed context](evidence/chief-responses-replay-context-probe-evidence.json): ReadTimeout45380ms, result/usage UNKNOWN. Причины тайм-аутов и прежних ошибок UNKNOWN; несколько параметров изменились, причинную связь не заявляем. Стоимость UNKNOWN.
- Atelier V5 YandexGPT5.1Pro +3Skills, no built-ins: generic UI error, FAIL этой попытки. V6 same model, reasoningOFF/noSkills/no built-ins/fresh context: STREAM_TIMEOUT, request-id `9cf76fbe-0b61-4abb-8d27-cb782d3dce9f`, [JSON evidence](evidence/chief-atelier-v6-preflight-evidence.json). Preflight FAIL, причина UNKNOWN; private screenshot в Git не включён. Это не доказательство непригодности A. Native binding NOT_VERIFIED, full acceptance NOT_RUN.

Пользователь явно разрешил временно переключить активный @UralbyteDemoBot с прежним token на isolated spike. Это заменяет прежнее требование separate bot для данного прогона; isolated DB/data, owner permissions и backend approvals сохраняются. Switch NOT_RUN. Token читает/использует только Root через штатную защищённую конфигурацию; в docs/model/capture его нет. Planner/code не переключали polling/публикацию.

[Limited capture plan](agent-runtime-spike-live-probe-plan.md) не меняет existing runtime: temporary noDB/noTelegramsecret endpoint, independent synthetic argument fingerprints, E1/E2 overlapping requests и swapped negatives. Header capability PASS не доказывает native Telegram identity и не включает SPIKE_BINDING_VERIFIED.

URALBYTE-13 и has-epic link25 к URALBYTE-3 подтверждены. PR/merge/live PASS не заявлены. Full Checker остаётся FAIL по невыполненным live gates; Reviewer NOT_RUN. Итог INCONCLUSIVE, рекомендация NOT_DECIDED; decision/delivery records ведёт Steward.

[Normalized messages context](evidence/chief-responses-normalized-context-probe-evidence.json): минимальные role/content assistant messages также дали ReadTimeout45312ms, response/usage/cost UNKNOWN. Диагностика context завершена; дальнейшие model retries не планируются. B function-call cycle PASS остаётся отдельным фактом от conversational continuation NOT_VERIFIED.
