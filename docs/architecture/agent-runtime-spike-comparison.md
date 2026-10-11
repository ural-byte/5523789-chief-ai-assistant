# Сравнение A/B/C: промежуточные результаты architecture spike

Planner: **TL;DR:** Изолированный backend adapter прошёл локальные проверки, но обязательная Telegram приёмка ещё NOT_RUN. Responses API впервые завершил настоящий function-call roundtrip: HTTP200 за 893 и 433 ms, 83 и 145 tokens. Отдельные stored/replayed context probes завершились тайм-аутами; причины UNKNOWN. Atelier V5 дал generic UI error; V6 без Skills завершился stream timeout. Оба preflight FAIL, причины UNKNOWN; непригодность архитектуры не доказана. Пользователь разрешил временный прогон активного @UralbyteDemoBot, но переключение NOT_RUN; сначала limited capture probe и safety gates. Итог INCONCLUSIVE, рекомендация NOT_DECIDED.

Подробный план

## Граница отчёта

Дата: **2026-10-11**. Основание — [согласованный план](agent-runtime-spike.md), [implementation plan](agent-runtime-spike-implementation-plan.md), [adapter и platform preflight](agent-runtime-spike-adapter.md), [независимый Checker](agent-runtime-spike-checker.md).

- A: Telegram → Agent Atelier → MCP/tools → backend.
- B: Telegram → backend → Yandex Responses API → tools.
- C: Telegram → existing worker → custom agent loop → model → tools.

Production runtime и deployment не изменялись. Локальный POC и синтетический API probe не заменяют настоящий Telegram owner callback. В таблице отдельно указаны факты о реализации, измеренные probes и ещё не выполненные gates. `NOT_TESTED` / `NOT_RUN` обозначают отсутствие проверки; `NOT_RETESTED` — отсутствие нового сопоставимого прогона C; `UNKNOWN` не означает ноль.

## Фактическое evidence

**A.** Private agent `chief-runtime-spike-20261011`, ID `aac99aa41o220jk4pqbq`. V1 DeepSeek 4 Flash: две попытки без успешного ответа; одна закончилась stream timeout. V2 Alice AI LLM Flash: контрольная попытка тоже без ответа. V3: три настоящих skills загружены и прикреплены. V4: отключены built-in Image Generation, Web Search и кодовый интерпретатор; контрольный запрос снова завершился UI ошибкой. Подробности, версии и хеши skills — в [adapter report](agent-runtime-spike-adapter.md). Это FAIL этих preflight attempts, а не доказанная причина ошибки или непригодность архитектуры. V5 YandexGPT5.1Pro с тремя Skills и без built-ins также дала generic UI error (FAIL этой попытки, причина UNKNOWN). V6 с YandexGPT5.1Pro, reasoningOFF, без Skills/built-ins и с fresh context завершился STREAM_TIMEOUT: request-id `9cf76fbe-0b61-4abb-8d27-cb782d3dce9f`, [JSON evidence](evidence/chief-atelier-v6-preflight-evidence.json). Это FAIL preflight, причина UNKNOWN, не доказательство непригодности A; private screenshot в Git не включён. MCP не подключён, агент не опубликован в Telegram.

**Исторические B probes.** [DeepSeek probe](evidence/chief-responses-probe-evidence.json): первый запрос ReadTimeout, **45424 ms**, response и usage неизвестны. [YandexGPT probe](evidence/chief-responses-yandexgpt-probe-evidence.json): первый запрос HTTP 200, **791 ms**, настоящий `probe_echo({"text":"Кедр"})`, **82 input + 13 output = 95 tokens**. Второй запрос с исходным input, function_call и соответствующим function_call_output закончился ReadTimeout, **45014 ms**. Это частичное подтверждение function calling, а не завершённый tool loop. [Дополнительный probe previous_response_id](evidence/chief-responses-yandexgpt-context-probe-evidence.json): первый запрос со store=true тоже дал HTTP 200 и function_call, **857 ms**, **82 input + 13 output = 95 tokens**. Второй запрос с previous_response_id и только function_call_output при tool_choice=none закончился ReadTimeout, **45004 ms**. Связывание выполнено реальным протокольным запросом, но завершённый roundtrip снова не доказан. Цена всех probes — UNKNOWN.

**Новые B probes — текущий результат.** [Official-shape roundtrip](evidence/chief-responses-official-probe-evidence.json) завершён: function_call HTTP200 **893 ms**, **70 input + 13 output = 83 tokens**; function_call_output continuation HTTP200 **433 ms**, **132 input + 13 output = 145 tokens** (cached input32), получен final weather answer. **PASS только function-call protocol roundtrip**, всего228 tokens двух успешных requests. Это не platform MCP loop, owner identity, Sasha/Lesha flow или Telegram acceptance. Несколько параметров относительно предыдущих попыток изменились; причина прежних timeout **UNKNOWN**.

[Stored context](evidence/chief-responses-context-official-probe-evidence.json): первый response HTTP200 **727 ms**, **28 input + 2 output = 30 tokens**; второй request ReadTimeout **45008 ms**, usage/result UNKNOWN. [Replayed context](evidence/chief-responses-replay-context-probe-evidence.json): ReadTimeout **45380 ms**, response/usage UNKNOWN. [Normalized messages context](evidence/chief-responses-normalized-context-probe-evidence.json), с минимальными role/content assistant messages: ReadTimeout **45312 ms**, response/usage UNKNOWN. Conversational continuity B не подтверждена. Диагностическая серия завершена; дополнительные model retries в следующий limited capture checkpoint не входят. Цена всех probes UNKNOWN; тайм-ауты сохранены и не скрыты успешным weather probe.


**Backend adapter.** Независимый полный pytest до correction cycle 1 — **578 PASS**, после исправления malformed callback.message весь spike suite — **51 PASS**; scoped Ruff/format и isolated startup — PASS. Проверены реальные PostgreSQL handlers и callback engine с mocked transport. Внешний binding gate закрыт и возвращает 503. Полный Checker — **FAIL** из-за невыполненных обязательных live gates; Reviewer — **NOT_RUN**. Эти результаты не подтверждают понимание follow-up моделью.

## Матрица: все 13 критериев

| Критерий | A: Atelier | B: Responses API | C: текущий runtime | Evidence и предел вывода |
| --- | --- | --- | --- | --- |
| Conversational quality | NOT_TESTED: model preflight без успешного ответа, точная цепочка NOT_RUN. | NOT_TESTED: echo function call не проверяет follow-up и конфликт. | NOT_RETESTED: свежая одинаковая цепочка не запускалась. | Нужны отдельные сообщения, показ конфликта, только «Лёша — правильный вариант», fresh retrieval после confirm. |
| Tool calling / multi-step loop | NOT_TESTED: MCP/tools не подключены. | Function-call roundtrip PASS: 893+433ms,83+145tokens. Platform MCP loop NOT_RUN; context probes timeout. | Registry/agent loop существуют; comparative live NOT_RETESTED. | Official roundtrip JSON плюс сохранённые failed attempts. Не доказано устранение client orchestration. |
| Skills / instructions | Upload/attachment PASS: три Skills в V3–V5; применение NOT_RUN. V6 noSkills preflight STREAM_TIMEOUT. | Skills/instructions behavior NOT_TESTED. | Instructions существуют; comparative behavior NOT_RETESTED. | IDs/SHA в adapter report; upload не доказывает применение. |
| Telegram integration / long polling | Агент не опубликован, тестовый бот не подключён: NOT_RUN. | Telegram adapter/chain NOT_RUN. | Existing long-poll worker остаётся baseline; новое измерение NOT_RETESTED. | В publication UI видны token и public/restricted allowlist; per-event identity/callback/ACK UNKNOWN. Снятие worker не доказано. |
| Control | Настройки модели, инструкции, skills и built-ins изменены в UI; native event binding UNKNOWN. | Параметры synthetic API запроса контролируются; end-to-end identity/stop/budget gate NOT_TESTED. | Existing limits, registry и domain guards доступны в коде; comparative control gate NOT_RETESTED. | Внешний adapter fail-closed. Общий MCP credential не доказывает owner/chat/source identity. |
| Approval safety | Local backend safety PASS; Atelier Telegram approval gate NOT_RUN. | Local backend safety PASS; Responses Telegram approval gate NOT_RUN. | Existing approval engine переиспользован и локально проверен; genuine fresh C gate NOT_RETESTED. | Checker: immutable keep/delete, ACK, exact callback, owner/chat, TTL, snapshots, cancel/replay. Model approved=true не является authority. |
| Observability | UI error/частичный request-id; V6 stream timeout/request-id, generation usage UNKNOWN. | Roundtrip latency/usage обоих requests сохранены; stored first727ms/30tokens, continuation/replay timeout usage UNKNOWN. | Existing ledger; comparative metrics NOT_MEASURED. | Все сохранённые JSON probes; нет p95/SLA по малой выборке. |
| Vendor lock-in / export | Версии агента и skills доступны; полная export/history/retention portability NOT_TESTED. | Программный Responses contract проверен частично; переносимость history/prompt/MCP NOT_TESTED. | Custom runtime/domain code доступны; стоимость смены provider и coupling NOT_MEASURED. | Наличие OpenAI-compatible API само по себе не доказывает переносимость поведения и состояния. |
| Complexity | Native bridge/MCP compatibility UNKNOWN; reduction NOT_MEASURED. | Function continuation protocol PASS; provider MCP loop/request-scoped headers/transport пока NOT_RUN. | Existing modules baseline; comparative complexity NOT_MEASURED. | Capture checkpoint ограничен synthetic endpoint, не runtime implementation. |
| Maintenance cost | NOT_MEASURED: только preflight и подготовка adapter. | NOT_MEASURED: ограниченные protocol probes. | NOT_MEASURED: historical effort не восстановлен. | Нет сопоставимых измерений активного времени сопровождения, диагностики и обновлений. |
| Стоимость эксплуатации | UNKNOWN: generation/hosting/MCP usage неполны. | UNKNOWN: roundtrip228tokens, stored first30tokens, historical95token attempts; timeout usage/charges UNKNOWN. | UNKNOWN для comparable flow. | Нет расчёта рублей; retries, embeddings, MCP/hosting и transport должны учитываться. |
| Доступность из РФ / ограничения | Signed-in UI доступен; V5 genericFAIL/V6 STREAM_TIMEOUT, причина UNKNOWN. | Real official roundtrip HTTP200; stored/replayed context timeout. Общая доступность UNKNOWN. | Existing bigbang сохранён; fresh comparative access NOT_RETESTED. | Тайм-аут не устанавливает региональную недоступность, quota или incompatibility. |
| Скорость разработки | NOT_MEASURED: агент/skills/adapter подготовлены, reliable active-time ledger отсутствует. | NOT_MEASURED: partial probe есть, active time не измерено. | NOT_MEASURED: историческое время неизвестно. | Есть конкретные deliverables, но нет основания объявлять вариант быстрее или дешевле в разработке. |

## Непройденные gates и конкретное продолжение

1. Пользователь явно разрешил временно использовать активный @UralbyteDemoBot; switch NOT_RUN. Прежний separate-bot prerequisite для этого прогона заменён новой авторизацией. До actual переключения нужен approved switch/rollback package; один consumer, isolated data, без публикации token. Сначала [limited capture checkpoint](agent-runtime-spike-live-probe-plan.md).
2. Доказать trusted per-event owner/chat/source binding, hidden metadata до tools/call, canonical delivery ACK и genuine callback конкретной карточки. Проверить MCP JSON Streamable HTTP subset adapter на реальном Atelier connector. До evidence внешний binding остаётся выключенным.
3. Диагностика generation/context завершена текущими probes: A preflight FAIL, причина UNKNOWN; B conversational continuity NOT_VERIFIED. Дальнейшие model retries не планируются в limited capture scope. Не менять conversational правила ради PASS.
4. Responses official roundtrip уже protocol PASS. Проверить request-scoped MCP header correlation limited capture probe с независимыми argument fingerprints/swapped negatives; context continuity остаётся NOT_VERIFIED. Не выдавать weather за Telegram acceptance.
5. После prerequisites подключить шесть capabilities, выполнить обязательную Telegram-цепочку, проверку skills/context/multi-step и отрицательные owner/approval cases. Owner самостоятельно подтверждает конкретный approval_id.
6. Собрать сопоставимое evidence A/B/C: end-to-end latency, model/tool calls, доступные tokens/cost, лишние уточнения, отсутствующие metrics и ограничения. При необходимости B использовать те же backend safeguards; не писать новую полную оркестрацию заранее.
7. Повторить независимый Checker на полном live gate, затем Reviewer по действующим правилам. Planner обновляет матрицу, Steward фиксирует recommendation; до решения пользователя миграцию не начинать.

## Decision gate

**Результат: INCONCLUSIVE. Рекомендация: NOT_DECIDED.**

При успешном A acceptance и совместимости с backend safety приоритет остаётся за Atelier. Для выбора B нужны конкретные доказанные ограничения A и успешный сопоставимый B. Для выбора C нужны объективные доказательства непригодности обоих альтернативных вариантов. Текущие UI ошибки и отдельные context тайм-ауты этих условий не выполняют; временное использование активного бота разрешено, но switch/live acceptance не выполнены.

Границы: без полной миграции, изменения production, переноса остальных tools, Gmail/Calendar/web search, удаления текущего runtime и выдачи simulated Telegram callbacks за genuine evidence.


## Tracker persistence

URALBYTE-13 подтверждён; has-epic link25 к URALBYTE-3 подтверждён. Это устраняет прежний TASK_KEY_PENDING blocker; не означает PR, live acceptance, merge или deployment. Decision/delivery updates ведёт Steward.
