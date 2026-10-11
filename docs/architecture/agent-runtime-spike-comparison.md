# Сравнение A/B/C: промежуточные результаты architecture spike

Planner: **TL;DR:** Подготовлен изолированный backend adapter; независимые локальные проверки проходят. В Atelier создан приватный агент, загружены и прикреплены три навыка, однако model probes не дали успешного ответа. Responses API вернул настоящий function_call на YandexGPT, но проверенный возврат function_call_output закончился тайм-аутом. Живая Telegram-цепочка и native identity/callback binding пока не проверены. Доказательств для выбора архитектуры недостаточно: **INCONCLUSIVE**, рекомендация **NOT_DECIDED**. Тайм-ауты не доказывают непригодность Atelier или Responses и не дают основания выбрать текущий runtime.

Подробный план

## Граница отчёта

Дата: **2026-10-11**. Основание — [согласованный план](agent-runtime-spike.md), [implementation plan](agent-runtime-spike-implementation-plan.md), [adapter и platform preflight](agent-runtime-spike-adapter.md), [независимый Checker](agent-runtime-spike-checker.md).

- A: Telegram → Agent Atelier → MCP/tools → backend.
- B: Telegram → backend → Yandex Responses API → tools.
- C: Telegram → existing worker → custom agent loop → model → tools.

Production runtime и deployment не изменялись. Локальный POC и синтетический API probe не заменяют настоящий Telegram owner callback. В таблице отдельно указаны факты о реализации, измеренные probes и ещё не выполненные gates. `NOT_TESTED` / `NOT_RUN` обозначают отсутствие проверки; `NOT_RETESTED` — отсутствие нового сопоставимого прогона C; `UNKNOWN` не означает ноль.

## Фактическое evidence

**A.** Private agent `chief-runtime-spike-20261011`, ID `aac99aa41o220jk4pqbq`. V1 DeepSeek 4 Flash: две попытки без успешного ответа; одна закончилась stream timeout. V2 Alice AI LLM Flash: контрольная попытка тоже без ответа. V3: три настоящих skills загружены и прикреплены. V4: отключены built-in Image Generation, Web Search и кодовый интерпретатор; контрольный запрос снова завершился UI ошибкой. Подробности, версии и хеши skills — в [adapter report](agent-runtime-spike-adapter.md). Это FAIL этих preflight attempts, а не доказанная причина ошибки или непригодность архитектуры. MCP не подключён, агент не опубликован в Telegram.

**B.** [DeepSeek probe](evidence/chief-responses-probe-evidence.json): первый запрос ReadTimeout, **45424 ms**, response и usage неизвестны. [YandexGPT probe](evidence/chief-responses-yandexgpt-probe-evidence.json): первый запрос HTTP 200, **791 ms**, настоящий `probe_echo({"text":"Кедр"})`, **82 input + 13 output = 95 tokens**. Второй запрос с исходным input, function_call и соответствующим function_call_output закончился ReadTimeout, **45014 ms**. Это частичное подтверждение function calling, а не завершённый tool loop. [Дополнительный probe previous_response_id](evidence/chief-responses-yandexgpt-context-probe-evidence.json): первый запрос со store=true тоже дал HTTP 200 и function_call, **857 ms**, **82 input + 13 output = 95 tokens**. Второй запрос с previous_response_id и только function_call_output при tool_choice=none закончился ReadTimeout, **45004 ms**. Связывание выполнено реальным протокольным запросом, но завершённый roundtrip снова не доказан. Цена всех probes — UNKNOWN.

**Backend adapter.** Независимый полный pytest до correction cycle 1 — **578 PASS**, после исправления malformed callback.message весь spike suite — **51 PASS**; scoped Ruff/format и isolated startup — PASS. Проверены реальные PostgreSQL handlers и callback engine с mocked transport. Внешний binding gate закрыт и возвращает 503. Полный Checker — **FAIL** из-за невыполненных обязательных live gates; Reviewer — **NOT_RUN**. Эти результаты не подтверждают понимание follow-up моделью.

## Матрица: все 13 критериев

| Критерий | A: Atelier | B: Responses API | C: текущий runtime | Evidence и предел вывода |
| --- | --- | --- | --- | --- |
| Conversational quality | NOT_TESTED: model preflight без успешного ответа, точная цепочка NOT_RUN. | NOT_TESTED: echo function call не проверяет follow-up и конфликт. | NOT_RETESTED: свежая одинаковая цепочка не запускалась. | Нужны отдельные сообщения, показ конфликта, только «Лёша — правильный вариант», fresh retrieval после confirm. |
| Tool calling / multi-step loop | NOT_TESTED: MCP/tools не подключены. | PARTIAL: YandexGPT вернул два начальных function_call; оба способа возврата результата timeout, loop не завершён. | Registry/agent loop существуют; сравнительный live прогон NOT_RETESTED. | Три JSON B probes. Шесть capabilities adapter подтверждены локально; это не agent tool calling. |
| Skills / instructions | Upload/attachment PASS: три навыка в V3/V4; применение моделью NOT_RUN. | Skills/instructions behavior NOT_TESTED. | Instructions/tool descriptions существуют; перенос и сопоставимое поведение NOT_RETESTED. | Skills IDs и SHA256 в adapter report. Загрузка не подтверждает выбор или выполнение навыка. |
| Telegram integration / long polling | Агент не опубликован, тестовый бот не подключён: NOT_RUN. | Telegram adapter/chain NOT_RUN. | Existing long-poll worker остаётся baseline; новое измерение NOT_RETESTED. | В publication UI видны token и public/restricted allowlist; per-event identity/callback/ACK UNKNOWN. Снятие worker не доказано. |
| Control | Настройки модели, инструкции, skills и built-ins изменены в UI; native event binding UNKNOWN. | Параметры synthetic API запроса контролируются; end-to-end identity/stop/budget gate NOT_TESTED. | Existing limits, registry и domain guards доступны в коде; comparative control gate NOT_RETESTED. | Внешний adapter fail-closed. Общий MCP credential не доказывает owner/chat/source identity. |
| Approval safety | Local backend safety PASS; Atelier Telegram approval gate NOT_RUN. | Local backend safety PASS; Responses Telegram approval gate NOT_RUN. | Existing approval engine переиспользован и локально проверен; genuine fresh C gate NOT_RETESTED. | Checker: immutable keep/delete, ACK, exact callback, owner/chat, TTL, snapshots, cancel/replay. Model approved=true не является authority. |
| Observability | Частичный request-id/UI error; generation usage, latency/cost completeness UNKNOWN. | HTTP status, latency и usage двух начальных YandexGPT ответов сохранены; timeout usage UNKNOWN, полного flow нет. | Existing AICall/Invocation и latency ledger; свежие сравнительные показатели NOT_MEASURED. | B JSON evidence; adapter сохраняет tool traces. Нельзя вычислить p95/SLA по единичному успешному вызову. |
| Vendor lock-in / export | Версии агента и skills доступны; полная export/history/retention portability NOT_TESTED. | Программный Responses contract проверен частично; переносимость history/prompt/MCP NOT_TESTED. | Custom runtime/domain code доступны; стоимость смены provider и coupling NOT_MEASURED. | Наличие OpenAI-compatible API само по себе не доказывает переносимость поведения и состояния. |
| Complexity | Adapter создан, но native bridge/MCP compatibility UNKNOWN; сокращение custom runtime NOT_MEASURED. | Один function call подтверждён; remaining continuation/transport code NOT_MEASURED. | Runtime/worker/backend guards остаются в baseline; сравнительный объём/сложность NOT_MEASURED. | Нет выполненной миграции: уменьшение количества модулей или правил не заявляется. |
| Maintenance cost | NOT_MEASURED: только preflight и подготовка adapter. | NOT_MEASURED: ограниченные protocol probes. | NOT_MEASURED: historical effort не восстановлен. | Нет сопоставимых измерений активного времени сопровождения, диагностики и обновлений. |
| Стоимость эксплуатации | UNKNOWN: generation usage/billing неполны; hosting/MCP flow не запущен. | UNKNOWN: по 95 tokens известны для двух начальных запросов; цену и timeout charges не определяли. | UNKNOWN для нового сопоставимого flow; прежние отчёты его не заменяют. | UNKNOWN cost не ноль. Полный расчёт включает retries, embeddings, MCP/hosting и транспорт. |
| Доступность из РФ / ограничения | Signed-in UI и создание агента доступны в текущем окружении; успешная generation/целевой deploy path NOT_TESTED. | Два HTTP 200 YandexGPT показывают доступность этого endpoint/model на момент probe; DeepSeek timeout, общая доступность UNKNOWN. | Existing bigbang baseline сохранён; новый сравнительный runtime/access probe NOT_RETESTED. | Тайм-аут не устанавливает региональную недоступность, quota issue или model incompatibility. |
| Скорость разработки | NOT_MEASURED: агент/skills/adapter подготовлены, reliable active-time ledger отсутствует. | NOT_MEASURED: partial probe есть, active time не измерено. | NOT_MEASURED: историческое время неизвестно. | Есть конкретные deliverables, но нет основания объявлять вариант быстрее или дешевле в разработке. |

## Непройденные gates и конкретное продолжение

1. Получить отдельный тестовый Telegram-бот с секретом вне переписки; не использовать production token и не создавать второй consumer рабочего бота.
2. Доказать trusted per-event owner/chat/source binding, hidden metadata до tools/call, canonical delivery ACK и genuine callback конкретной карточки. Проверить MCP JSON Streamable HTTP subset adapter на реальном Atelier connector. До evidence внешний binding остаётся выключенным.
3. Установить причину model preflight failures ограниченным диагностическим probe с request IDs; не менять backend conversational правила ради успешного ответа.
4. Завершить Responses roundtrip на корректном protocol path, сохранив все попытки и usage. Успешный echo будет только protocol PASS, не Telegram acceptance PASS.
5. После prerequisites подключить шесть capabilities, выполнить обязательную Telegram-цепочку, проверку skills/context/multi-step и отрицательные owner/approval cases. Owner самостоятельно подтверждает конкретный approval_id.
6. Собрать сопоставимое evidence A/B/C: end-to-end latency, model/tool calls, доступные tokens/cost, лишние уточнения, отсутствующие metrics и ограничения. При необходимости B использовать те же backend safeguards; не писать новую полную оркестрацию заранее.
7. Повторить независимый Checker на полном live gate, затем Reviewer по действующим правилам. Planner обновляет матрицу, Steward фиксирует recommendation; до решения пользователя миграцию не начинать.

## Decision gate

**Результат: INCONCLUSIVE. Рекомендация: NOT_DECIDED.**

При успешном A acceptance и совместимости с backend safety приоритет остаётся за Atelier. Для выбора B нужны конкретные доказанные ограничения A и успешный сопоставимый B. Для выбора C нужны объективные доказательства непригодности обоих альтернативных вариантов. Текущие UI ошибки, единичные API тайм-ауты и отсутствие тестового бота этих условий не выполняют.

Границы: без полной миграции, изменения production, переноса остальных tools, Gmail/Calendar/web search, удаления текущего runtime и выдачи simulated Telegram callbacks за genuine evidence.
