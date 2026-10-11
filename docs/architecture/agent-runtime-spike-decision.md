# Промежуточный decision gate: выбор agent runtime

Steward: **INCONCLUSIVE. Рекомендация: NOT_DECIDED.** Дата: 2026-10-11.
Architecture spike не завершён. Текущие результаты не обосновывают миграцию
на Atelier, выбор Responses API или отказ от обоих в пользу текущего runtime.

Проект: [ural-byte/5523789-chief-ai-assistant](https://github.com/ural-byte/5523789-chief-ai-assistant).
Существующий заказ: [URALBYTE-3](https://tracker.yandex.ru/URALBYTE-3).
Изолированный workspace: ветка `codex/agent-runtime-spike`, base `4ccfe10`.
Основание решения — [согласованный план](agent-runtime-spike.md),
[сравнение Planner по 13 критериям](agent-runtime-spike-comparison.md),
[реализация и platform evidence](agent-runtime-spike-adapter.md),
[независимый Checker](agent-runtime-spike-checker.md).

## Что подтверждено

- Подготовлен изолированный runnable backend adapter с шестью capabilities через
  реальные PostgreSQL handlers. Локально проверены owner/chat isolation,
  immutable keep/delete, доставка канонической карточки и ACK, exact callback,
  отмена, TTL, replay, snapshots и fencing. Внешний binding по умолчанию закрыт.
  Fixture binding и mocked Telegram transport не являются native Atelier proof.
- Независимый полный suite до correction cycle 1: **578 PASS**, 204.89 с.
  После исправления malformed callback.message: весь spike suite **51 PASS**,
  11.81 с; scoped Ruff/format и isolated startup PASS. Локальных blocking code
  defects Checker не обнаружил.
- В Atelier создан private agent; три настоящих Skills загружены, прикреплены
  и сохранены в V3. В V4 выключены Image Generation, Web Search и кодовый
  интерпретатор. Manifest IDs/SHA256 сохранён в adapter report. Это подтверждает
  upload/attachment, но не применение навыков моделью.
- Для B два начальных YandexGPT запроса вернули настоящие function_call:
  [первый probe](evidence/chief-responses-yandexgpt-probe-evidence.json) — HTTP 200,
  791 мс, 95 tokens; [previous_response_id probe](evidence/chief-responses-yandexgpt-context-probe-evidence.json)
  — HTTP 200, 857 мс, 95 tokens. Передача function_call_output проверена реальными
  запросами, но успешного завершения roundtrip не получено.

## Почему выбор пока невозможен

**A / Atelier.** V1 DeepSeek дважды, V2 Alice и V4 после отключения built-ins
не дали успешного assistant answer. V4 показала «Не удалось получить ответ
агента». Причина этих ошибок не установлена. MCP не подключён; native publication,
отдельный тестовый Telegram-бот, trusted per-event binding, genuine callback/ACK,
conversational context, применение Skills и multi-step loop не проверены.
Это не доказательство архитектурной непригодности A.

**B / Responses.** Продолжение первого YandexGPT function call завершилось
ReadTimeout 45014 мс; продолжение с previous_response_id — ReadTimeout 45004 мс.
[DeepSeek probe](evidence/chief-responses-probe-evidence.json) завершился
ReadTimeout 45424 мс уже на первом запросе. Успешный полный tool roundtrip,
follow-up и Telegram acceptance отсутствуют. Тайм-ауты не устанавливают причину,
региональную недоступность, несоответствие модели или непригодность архитектуры.
Usage тайм-аутов и полная стоимость UNKNOWN; это не нулевые значения.

**C / текущий runtime.** Существующая реализация сохранена как рабочая база.
Свежий сопоставимый live прогон и измерения не выполнены. Прежняя поставка
и локальные тесты не дают основания автоматически выбрать C. Объективная
непригодность A и B не доказана.

Общий **Checker: FAIL**: обязательная genuine Telegram-цепочка и проверка
platform behavior **NOT_RUN**. **Reviewer: NOT_RUN** по согласованной
последовательности gates. Локальный PASS не является полным PASS spike.
Матрица Planner сохраняет UNKNOWN/NOT_TESTED/NOT_RETESTED для отсутствующих
показателей; предпочтение нельзя выводить из удобства уже имеющегося кода.

## Условия для следующего решения

| Возможная рекомендация | Необходимое основание | Текущий вердикт |
| --- | --- | --- |
| MIGRATE TO AGENT ATELIER | A проходит genuine Telegram и backend safety gates; control/observability достаточны. При равной пригодности приоритет A. | Основание не получено. |
| USE RESPONSES API | Установлены конкретные ограничения A; B проходит ту же цепочку и safeguards. | Основание не получено. |
| KEEP CURRENT RUNTIME | Объективная непригодность A и B подтверждена POC или конкретным блокирующим контрактом. | Основание не получено. |

До получения этих оснований действует **NOT_DECIDED / INCONCLUSIVE**.
Это промежуточный результат исследования, без разрешения на полную миграцию.

## Передача следующего этапа

Следующие gates сформулированы Planner в comparison report; Steward сохраняет
их для продолжения, не выбирая новую архитектуру:

1. Получить отдельный тестовый Telegram-бот со штатным хранением секрета;
   production token и consumer рабочего бота не используются.
2. Проверить реальный Atelier connector и trusted owner/chat/source event binding,
   передачу hidden metadata, canonical delivery ACK и callback конкретного
   approval_id/message_id. Внешний adapter остаётся закрытым до такого evidence.
3. Ограниченным provider probe установить причину model preflight failures;
   завершить Responses roundtrip с protocol evidence. Успех echo подтверждает
   только протокол. Backend conversational rules ради PASS не добавляются.
4. Подключить шесть capabilities и пройти отдельными сообщениями точную живую
   цепочку: Саша/Лёша → конфликт → «Лёша — правильный вариант» → backend кнопки →
   настоящий owner confirm → fresh retrieval только Лёши. Проверить Skills,
   context/multi-step и отрицательные owner/approval случаи. Callback не симулировать.
5. Собрать сопоставимые измерения A/B/C; повторить независимый Checker на полном
   gate, после PASS провести Reviewer. Planner обновляет сравнение, Steward
   сохраняет основанную на evidence рекомендацию; решение о миграции принимает
   пользователь отдельно.

Текущий handoff — **ожидание внешних prerequisites и полного live evidence**.
Бесконечное расширение текущего runtime, миграция, production deployment и
закрытие прежних тикетов этим документом не разрешены.

## Долговременное состояние и ограничения поставки

Согласованный приоритет сохранён в URALBYTE-12, комментарий **100**,
longId `6acb645920b4f92791265df0`, 2026-10-11T10:26:33Z. Его не повторять.
Новый delivery-комментарий с результатами **NOT_POSTED**: обе попытки HTTP 429.
Создание самостоятельного spike с неизменным creation key
`chief-agent-runtime-spike-2026-10-11` также HTTP 429, включая повтор после
примерно четырёх часов; ключ/receipt и связь has-epic не подтверждены.
Свежий whoami подтвердил qdinov и cloud org bpf53toodvl9r97aasc2; проверка
AI-доступа остаётся включённой. В этой итерации дальнейшие Tracker retries остановлены.

[Подготовленное delivery update](agent-runtime-spike-delivery-pending.md) и этот
документ позволяют продолжить работу, но не заменяют запись состояния в Трекере.
После восстановления API повторять создание только с тем же creation key и
аргументами, затем подтвердить связь с URALBYTE-3 и сохранить актуальные результаты.
PR не создаётся до подтверждённого ключа задачи. Статусы прежних задач не менялись;
spike не отмечен завершённым. Надёжных полных интервалов активной работы по ролям
нет, worklog и стоимость задним числом не выдуманы.
