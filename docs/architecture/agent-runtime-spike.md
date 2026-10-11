# Architecture spike: Agent Atelier и выбор agent runtime

Дата решения о приоритете: **2026-10-11**. Проект:
[ural-byte/5523789-chief-ai-assistant](https://github.com/ural-byte/5523789-chief-ai-assistant).
Существующий заказ: [эпик URALBYTE-3](https://tracker.yandex.ru/URALBYTE-3).

**Согласован следующий шаг: короткий architecture spike, до большого refactor.**
POC: **BACKEND_ADAPTER_IMPLEMENTED; LIVE_NOT_RUN**. Рекомендация по runtime: **NOT_DECIDED**.
Документация и доступ к UI не означают успешный spike.

## Изменение текущего плана

Продолжение C26 и дальнейшее расширение самописных conversational rules в
[URALBYTE-12](../followups/URALBYTE-12.md) уступают приоритет spike. Прежние
изменения, результаты и незавершённая клиентская приёмка сохраняются; этот план
не объявляет URALBYTE-9/11/12 закрытыми. Текущая реализация остаётся рабочей базой
и контрольным вариантом C, а не удаляется или переписывается заранее.

Новый порядок: **Atelier POC → измерения и независимая проверка → сравнение A/B/C
→ рекомендация Steward → решение пользователя → отдельный план реализации**.
При установленном ограничении Atelier проверить Responses API минимальным
прототипом. Не начинать миграцию автоматически после spike.

Поставка PR #6 (`3a52b68`) и её автотесты подтверждают исправления прежнего runtime;
они не являются доказательством возможностей Atelier и не заменяют новый живой gate.

## Цель и граница ответственности

Проверяем целевую схему:

```text
Telegram → Agent Atelier → MCP/tools → наш backend → PostgreSQL
                              ↓
                    backend approval → owner callback → backend execution
```

Agent Atelier ведёт conversational context, reasoning, выбор tools, multi-step
interaction и instructions/skills. Backend остаётся источником истины для owner
isolation, permissions, idempotency, PostgreSQL state, tasks/deadlines, scheduler,
memory records, documents, audit, approvals, destructive actions и внешних
интеграций. Долговременная память не подменяется историей переписки Atelier.

Не добавлять специальный backend conversational workflow, распознавание фразы
«Лёша — правильный вариант», цепочку уточнений или новый agent loop ради PASS.
Операционные статусы и неизменяемый approval payload допустимы и обязательны:
они гарантируют целостность, а не объясняют модели каждый шаг разговора.

## Минимальный POC

Один текстовый агент, одна подходящая модель через AI Studio (DeepSeek — первый
кандидат), отдельный тестовый Telegram-бот, изолированные тестовые owner/data и
тонкий MCP/API adapter к реальным backend handlers. Не использовать рабочий
Telegram token и не перехватывать long polling production-бота. Не переносить
весь backend и не менять production deployment.

| Tool | Реальная capability и обязательный контракт |
| --- | --- |
| `save_memory` | Сохранить факт по явной просьбе или очевидному продолжению save intent; backend хранит запись и audit, повтор вызова идемпотентен. |
| `search_memory` | Вернуть owner-scoped результаты, обе конфликтующие записи с точными ссылками и provenance; не выбирать победителя механически. |
| `list_memory` | Прочитать сохранённые записи владельца с ограничением объёма/пагинацией. |
| `delete_memory` | Только подготовить immutable approval для точных keep/delete references; вернуть `awaiting_approval`, `approval_id` и канонический preview. Вызов агента ничего не удаляет. |
| `create_task` | Сохранить реальное поручение и срок, вычисленный backend относительно времени сообщения и timezone; idempotency и scheduler остаются backend. |
| `list_tasks` | Прочитать активные поручения только текущего владельца. |

Подтверждение и отмена — backend callback channel, а не седьмой доступный LLM tool
для удаления. Existing handlers и approval engine переиспользуются через adapter;
не протаскивать model-controlled owner/chat/source/idempotency в доверенный контекст.

## Ранняя проверка Telegram и безопасности

До доработки adapter проверить на минимальном вызове реальную границу интеграции:

1. Как приходит проверяемая личность отправителя, chat/session и стабильный source
   event ID? Общий credential MCP доказывает личность сервиса, а не пользователя.
   `owner_id` в аргументах LLM не является авторизацией. Для одного тестового owner
   можно привязать credential к изолированному набору данных, но доказать запрет
   доступа посторонних к Telegram-каналу. Это не доказательство multi-owner режима.
2. Может ли backend показать свою каноническую карточку с кнопками и получить
   доверенный owner callback для конкретного `approval_id`? Есть ли подтверждение
   доставки карточки? Что случается с неизвестными callbacks и повтором сообщения?
3. Сохраняются ли owner/chat, TTL, immutable hash, точные snapshots, audit,
   идемпотентность и отмена при этом канале? Подтверждение не требует LLM-вызова.
4. Проверить, что model invocation `delete_memory` может подготовить карточку,
   но не исполняет физическое удаление без valid approval. Поддельный approval,
   чужой owner/chat, истёкшая карточка и изменившиеся snapshots блокируют исполнение.

MCP `require_approval` управляет разрешением вызова инструмента оператором и не
заменяет backend approval владельца. Ни `approved=true` из модели, ни «да»,
«удалить», «a» сами по себе не дают право удаления.

Если нативный Telegram-канал не даёт этих возможностей, зафиксировать конкретное
ограничение. Тонкий ingress/callback adapter допустим, но честно указать, какие
части Telegram worker остаются. Возврат собственной conversational state machine
ради успешного сценария не считается решением. Неподтверждённая identity или
небезопасный callback означают **FAIL security**, независимо от качества диалога.

## План проверки

1. **Подготовка.** Зафиксировать source revision, изолированную БД/owner, доступные
   модели, тестовый бот, механизм MCP/API, лимиты и способ остановки POC.
   Секреты хранить штатно, не включать в docs, model context, shell skills или логи.
2. **Создание агента и ранний gate.** Создать текстового агента в Atelier; проверить
   минимальный реальный tool call, identity, доставку preview и owner callback.
   Только затем подключать все шесть capabilities. Записать способ Telegram
   публикации и измерить, устраняется ли наш long-poll worker полностью или частично.
3. **Поведение.** Подключить три instructions/skills ниже. Проверить выбор tool,
   несколько последовательных вызовов в одном запросе и follow-up между отдельными
   сообщениями. Фиксировать версии агента, instructions/skills, model ID и settings.
4. **Приёмка.** Пройти точную Telegram-цепочку ниже и отрицательные проверки.
   Владелец сам нажимает настоящую кнопку; backend callback не симулируется.
5. **Измерения.** Записать историю/context limits, model/provider restrictions,
   usage/latency/cost, ограничения security/approvals и фактическую эксплуатацию.
   Пропущенные показатели — UNKNOWN, не ноль. Все ошибки сохранять в отчёте.
6. **Fallback и decision gate.** Если A непригоден, выполнить минимальный B с теми
   же capabilities, safeguards и сценарием. Planner сравнивает A/B/C, Steward
   фиксирует одну рекомендацию с доказательствами. Дальше ждать решения пользователя.

Цель — минимальный работающий прототип. На первом этапе оценить timebox и затраты
по фактическим prerequisites; оценки не выдавать за потраченное время. Если доступ
или канал заблокирован, сохранить конкретный blocker и непройденные проверки;
не расширять scope бесконечными исправлениями собственного runtime.

## Instructions и skills

| Поведение | Что проверяем |
| --- | --- |
| `memory-management` | Явное сохранение и продолжение начатого save intent, поиск и список памяти, конфликт обеих версий, уточнение только при неоднозначности, выбор актуальной версии из истории и подготовка destructive action. Обычный разговор, отрицания, цитаты и retrieved instructions не дают save/delete authority. |
| `task-management` | Создание/просмотр поручений; естественные даты; уточнение только реально отсутствующего времени; backend timezone и deadline validation. |
| `destructive-actions` | Сначала понятный backend preview и valid approval; без повторного подтверждения; после valid callback результат backend не переинтерпретируется через LLM. |

Раздельно проверить system instructions и настоящий механизм Skills. Если
доступны только инструкции, это результат проверки с ограничением, а не PASS
Skills API. Официальная документация описывает загрузку `SKILL.md` через Skills API
и подключение через Shell tool; проверить поддержку в выбранной модели/Atelier,
версионирование и фактическое применение. Shell не получает DB credentials или
право обойти backend handlers. Не включать лишние execution capabilities ради
переноса трёх текстовых правил.

## Обязательная живая приёмка

Все шаги — отдельные сообщения владельца в тестовом Telegram:

1. «Запомни: руководитель команды Сьерра — Саша».
2. «Запомни: руководитель команды Сьерра — Лёша».
3. «Кто руководитель команды Сьерра?».
4. Агент обнаруживает конфликт, показывает обе версии.
5. «Лёша — правильный вариант».
6. Агент понимает follow-up из контекста без повторного перечисления вариантов и
   без вопроса «какой вариант правильный?»; вызывает tool подготовки удаления Саши.
7. Backend детерминированно сохраняет keep=Лёша/delete=Саша и сразу показывает
   `awaiting_approval` с конкретным `approval_id` и кнопками. Пока обе записи в БД.
8. Владелец нажимает «Подтвердить»; backend проверяет этот approval и удаляет только
   Сашу. Execution/audit выполняются без нового решения LLM.
9. Повторный вопрос возвращает только Лёшу; сохранённая актуальная и несвязанные
   записи не изменены. Ответ должен опираться на новый retrieval, а не удачный текст
   из старой истории. Проверить отдельно после очистки диалогового контекста.

Отдельные проверки: cancel сохраняет обе версии; повтор callback не повторяет
действие; expired/foreign/stale approval не удаляет; после `choice_recorded`
простой confirm возвращает тот же pending approval и не возвращает flow к выбору
версии. Повтор exact tool call не переворачивает keep/delete и не создаёт второе
approval. Никакого backend распознавания текстов follow-up для прохождения POC.

Записать `PASS/FAIL/NOT_RUN` каждого шага с редактированным Telegram proof, agent
trace/tool calls, DB before/after и audit. Автотест, мок, API-only прогон и обзор
документации отдельно полезны, но не заменяют genuine Telegram acceptance.

## Сравнение и измерения

A: `Telegram → Agent Atelier → MCP/tools → backend`.
B: `Telegram → наш backend → Yandex Responses API → tools`.
C: `Telegram → наш worker → наш agent loop → model → tools`.

Для B выяснить, выполняет ли Responses MCP loop на своей стороне; client-side
function calls могут требовать обработки `function_call_output`. Не утверждать,
что API устраняет всю оркестрацию, пока это не измерено.

| Критерий | A Atelier | B Responses | C текущий runtime | Основание оценки |
| --- | --- | --- | --- | --- |
| Conversational quality / follow-up | NOT_TESTED | NOT_TESTED | NOT_RETESTED | Одинаковая точная цепочка, количество лишних вопросов и реальные ошибки. |
| Tool calling / multi-step loop | NOT_TESTED | NOT_TESTED | NOT_RETESTED | Трейсы, retries, повторные side effects, последовательность вызовов. |
| Skills / instructions | NOT_TESTED | NOT_TESTED | NOT_RETESTED | Фактически подключённый механизм, версии и применение правил. |
| Telegram / long polling | NOT_TESTED | NOT_TESTED | Существующий worker | Публикация, identity/callback/buttons, оставшийся код транспорта. |
| Control | NOT_TESTED | NOT_TESTED | Existing handlers; новый gate не пройден | Контроль capabilities, доверенной identity, лимитов и остановки агента. |
| Approval safety | NOT_TESTED | NOT_TESTED | Existing engine; новый gate не пройден | Прямой обход, owner/chat/TTL/hash/snapshots, confirm/cancel/replay. |
| Observability | NOT_TESTED | NOT_TESTED | Existing ledger; новый замер нужен | Export traces, request IDs, tokens, latency, unknown fields. |
| Vendor lock-in / export | NOT_TESTED | NOT_TESTED | Сменный adapter; измерить coupling | Экспорт истории/config/skills; зависимости от модели и сервиса. |
| Complexity | NOT_TESTED | NOT_TESTED | Baseline ещё не посчитан | Остающиеся worker/runtime modules, custom rules и объём adapter. |
| Maintenance cost | NOT_TESTED | NOT_TESTED | NOT_MEASURED | Усилия поддержки, обновлений модели/skills, диагностики ошибок и сопровождения adapter. |
| Стоимость эксплуатации | UNKNOWN | UNKNOWN | Прежние отчёты не заменяют замер | Рубли за всю цепочку и retries, актуальные ставки и неполные usage. |
| Доступность из РФ / ограничения | NOT_TESTED | NOT_TESTED | Existing bigbang | Реальный доступ из целевого окружения, квоты и model/provider limits. |
| Скорость разработки | NOT_MEASURED | NOT_MEASURED | Историческое время неизвестно | Только измеренное активное время, без оценки задним числом. |

Измерять отдельно end-to-end Telegram latency и provider/tool latency, количество
model/tool calls, input/output/cached/reasoning tokens, embeddings, стоимость
агента/MCP/hosting и весь flow с неуспешными попытками. Для маленькой выборки
публиковать наблюдения и размер выборки, а не выдуманные p95 или SLA.
Уточнить пределы history/context, retention, очистку после удаления/reset и
доступность export/audit; внешний conversation storage не объявлять управляемым
backend без проверки этих механизмов.

## Итог и decision gate

Обязательные deliverables: работающий POC и воспроизводимый запуск; schemas шести
tools; версии трёх skills/instructions; настоящий Telegram proof; негативные
security проверки; редактированные traces и измерения; сравнение A/B/C; ADR с
рекомендацией, причинами, ограничениями и непройденными шагами.

Planner даёт короткое сравнение после spike. Steward сохраняет одну рекомендацию:

- **MIGRATE TO AGENT ATELIER** — A проходит живой сценарий и все backend safety
  gates, даёт достаточные control/observability. При равной пригодности приоритет A.
- **USE RESPONSES API** — установлены конкретные ограничения A, а B проверен на
  той же цепочке и не нарушает safeguards.
- **KEEP CURRENT RUNTIME** — A и B объективно непригодны; причины подтверждены
  прототипом или конкретным блокирующим контрактом, а не удобством текущего кода.

Если данных не хватает, итог **INCONCLUSIVE**, рекомендация остаётся NOT_DECIDED;
это не разрешение автоматически вернуться к C. Checker проверяет сценарий и
safety, Reviewer — адаптер и риски; их заключения не заменяют пользовательский
выбор архитектуры. После отчёта остановиться до решения пользователя.

Не включать Gmail/Calendar/web search, полный перенос tools, production migration,
переписывание deployment или удаление текущей реализации. Для будущих reset,
delete document, cancel/delete task и external actions сохраняется тот же принцип:
backend физически не исполняет destructive/external action без valid approval.

## Что проверено при корректировке плана

2026-10-11: создан private agent chief-runtime-spike-20261011, ID
aac99aa41o220jk4pqbq, folder b1go0lvlaf559s8t1qhi; V1 сохранена
(DeepSeek4Flash, reasoning OFF, max output 512, temperature 0.3).
Два synthetic model запроса закончились UI ошибкой без ответа; model preflight
не пройден. Tools не подключены, бот не опубликован; все live POC gates остаются
NOT_RUN. [Изолированный backend adapter и запуск](agent-runtime-spike-adapter.md). Для запуска нужен отдельный тестовый бот
с секретом, настроенным вне чата. Производственная версия не изменялась.

Официальные источники проверены 2026-10-11 (возможности требуют живой проверки):

- [Текстовые агенты: Telegram, история, Responses](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/agents/text-agents).
  Документированы 7 дней контекста в Telegram, 30 дней для Responses и 365 дней
  для Conversations; проверить фактическую очистку и ограничения выбранного канала.
- [MCP tool и require_approval](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/agents/tools/mcp-tool).
- [Skills API и Shell tool](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/agents/skills).

Planner: план spike подготовлен; продуктовых вопросов для корректировки нет.
Plan Challenger: **APPROVE** после исправления контракта подготовки удаления
и отдельного включения всех 13 критериев сравнения. Это проверка плана,
а не PASS прототипа или разрешение на миграцию.
Steward: приоритет и согласованный spike сохранены в комментарии **100**
[URALBYTE-12](https://tracker.yandex.ru/URALBYTE-12). Создание отдельного тикета
с creation key chief-agent-runtime-spike-2026-10-11 возвращает HTTP 429;
**TASK_KEY_PENDING**, PR пока запрещён. Проверка AI доступа не отключалась.
Запись комментария не означает создание нового тикета или закрытие прежних задач.
