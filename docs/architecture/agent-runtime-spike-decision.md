# Промежуточный decision gate: выбор agent runtime

Steward: **INCONCLUSIVE. Рекомендация: NOT_DECIDED.** Дата: 2026-10-11.
Architecture spike не завершён. Текущие результаты не обосновывают миграцию
на Atelier, выбор Responses API или отказ от обоих в пользу текущего runtime.

Проект: [ural-byte/5523789-chief-ai-assistant](https://github.com/ural-byte/5523789-chief-ai-assistant).
Существующий заказ: [URALBYTE-3](https://tracker.yandex.ru/URALBYTE-3).
Самостоятельный spike: [URALBYTE-13](https://tracker.yandex.ru/URALBYTE-13); создание и связь с эпиком подтверждены receipts.
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
  запросами; обе ранние попытки continuation завершились timeout.
- Дополнительный [faithful official-shape B probe](evidence/chief-responses-official-probe-evidence.json)
  с default auto и model URI YandexGPT без `/latest` завершил полный синтетический
  roundtrip: function_call HTTP 200 за 893 мс, 83 tokens; конечный assistant answer
  HTTP 200 за 433 мс, 145 tokens. Это протокольный PASS этой попытки; причина
  ранних timeout UNKNOWN, никакой отдельный параметр не объявляется доказанной причиной.

## Почему выбор пока невозможен

**A / Atelier.** V1 DeepSeek дважды, V2 Alice и V4 после отключения built-ins
не дали успешного assistant answer. V4 показала «Не удалось получить ответ
агента»; V5 YandexGPT с тремя Skills также FAIL. V6 YandexGPT 5.1 Pro без Skills/built-ins
и с fresh context завершилась **FAIL preflight**: stream timeout, request-id
`9cf76fbe-0b61-4abb-8d27-cb782d3dce9f`; успешного assistant answer нет.
[Sanitized V6 evidence](evidence/chief-atelier-v6-preflight-evidence.json).
Screenshot сохранён локально: `data/spike-evidence/atelier-v6-preflight.jpg`
в исходном checkout. Эта ошибка также не доказывает непригодность A. Причины
ошибок UNKNOWN. MCP/native publication, trusted per-event binding, genuine
callback/ACK, применение Skills и multi-step behavior не подтверждены.
Это не доказательство архитектурной непригодности A.

**B / Responses.** Теперь подтверждён один полный синтетический tool roundtrip
faithful official-shape запроса. Ранние continuation timeout 45014/45004 мс и
DeepSeek timeout 45424 мс сохраняются как отдельные наблюдения. В новом
[stored-context probe](evidence/chief-responses-context-official-probe-evidence.json)
первое сообщение получило HTTP 200, 727 мс, 30 tokens, а продолжение с
previous_response_id — ReadTimeout 45008 мс. [Input history replay](evidence/chief-responses-replay-context-probe-evidence.json)
также ReadTimeout 45380 мс. Последняя [диагностика с нормализованными messages](evidence/chief-responses-normalized-context-probe-evidence.json)
завершилась ReadTimeout 45312 мс; дальнейшие модельные retries остановлены.
Успешное context continuation, точный follow-up и
Telegram acceptance пока не получены. Причины timeout UNKNOWN. Частичный
протокольный успех не является доказанной пригодностью B для согласованной цепочки.
Usage тайм-аутов и полная стоимость UNKNOWN; это не нулевые значения.

**C / текущий runtime.** Существующая реализация сохранена как рабочая база.
Свежий сопоставимый live прогон и измерения не выполнены. Прежняя поставка
и локальные тесты не дают основания автоматически выбрать C. Объективная
непригодность A и B не доказана.

Последний полный **Checker: FAIL**: обязательная genuine Telegram-цепочка и проверка
platform behavior **NOT_RUN**. **Reviewer: NOT_RUN** по согласованной
последовательности gates. Локальный PASS не является полным PASS spike.
Матрица Planner сохраняет UNKNOWN/NOT_TESTED/NOT_RETESTED для отсутствующих
показателей; предпочтение нельзя выводить из удобства уже имеющегося кода.

## Checkpoint CI и внешнего capture preflight

[Draft PR7](https://github.com/ural-byte/5523789-chief-ai-assistant/pull/7) открыт
с base main и head `codex/agent-runtime-spike`. Для точного checkpoint
`c908f98c3332ce3cb84bebd53f10d1fe49daa3df` обе CI checks **SUCCESS**.
[Run 38151798051](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/38151798051):
**593 PASS**, 7 warnings, 265.97 с; [sanitized CI evidence](evidence/chief-spike-ci-c908f98-evidence.json).
Это проверка указанного head; текущая доработка capture probe ещё не покрыта
этим утверждением. Полный live gate и независимый Reviewer остаются непройденными.

В ограниченном Cloud Functions preflight [первая попытка](evidence/chief-cloud-runtime-read-evidence.json)
подтвердила configured IAM HTTP 200, затем официальный GET runtimes вернул
HTTP 503; общая latency 1242 мс. [Один повтор](evidence/chief-cloud-runtime-read-repeat-evidence.json)
снова получил IAM HTTP 200, затем GET runtimes ReadTimeout с лимитом 12 с;
общая latency 13046 мс. Причина UNKNOWN. Это только read-only preflight:
ресурсы и endpoint **не созданы**, actual MCP capture и genuine Telegram
**NOT_RUN**, `@UralbyteDemoBot` **NOT_SWITCHED**. Доступность IAM не доказывает
готовность Cloud Functions runtime или публикации endpoint.

Capture probe прошёл round 2 Plan Challenger APPROVE. Hardworker завершил
ограниченный probe/CLI/tests; независимый scoped Checker: **87 PASS**, 12.43 с
(51 прежний + 36 новых), scoped Ruff/format PASS; standalone ZIP unauthenticated
request возвращает 401 — PASS. Новых локальных defects не обнаружено.
Reviewer **NOT_RUN**. Успех сборки/CI прежнего head и read-only preflight не
выдаются за native binding PASS. Модельные retries остановлены.

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

1. Выполнить безопасно подготовленный Telegram transport probe. Пользователь
   отдельно разрешил временно переключить действующего `@UralbyteDemoBot` на spike
   с его активным token и затем вернуть. Это явное исключение из первоначального
   требования отдельного тестового бота. На момент обновления переключение **не
   подтверждено**; секрет не включается в документы. Перед действием зафиксировать
   восстановление транспорта и исключить одновременно работающих consumers.
   Разрешение на этот временный тест не означает полную миграцию.
2. Проверить реальный Atelier connector и trusted owner/chat/source event binding,
   передачу hidden metadata, canonical delivery ACK и callback конкретного
   approval_id/message_id. Внешний adapter остаётся закрытым до такого evidence.
3. Ограниченным provider probe установить причину model preflight failures;
   проверить успешное context continuation и multi-step behavior. Один
   синтетический Responses roundtrip уже PASS; он подтверждает только протокол. Backend conversational rules ради PASS не добавляются.
4. Подключить шесть capabilities и пройти отдельными сообщениями точную живую
   цепочку: Саша/Лёша → конфликт → «Лёша — правильный вариант» → backend кнопки →
   настоящий owner confirm → fresh retrieval только Лёши. Проверить Skills,
   context/multi-step и отрицательные owner/approval случаи. Callback не симулировать.
5. Собрать сопоставимые измерения A/B/C; повторить независимый Checker на полном
   gate, после PASS провести Reviewer. Planner обновляет сравнение, Steward
   сохраняет основанную на evidence рекомендацию; решение о миграции принимает
   пользователь отдельно.

Текущий handoff — **возобновлённый spike: native binding probe и полный live evidence**.
Orchestrator возобновил инженерное планирование native binding probe.
Полная миграция, постоянное переключение production и закрытие прежних тикетов
этим документом не разрешены; временный тест указанного бота отдельно авторизован.

## Долговременное состояние и ограничения поставки

Согласованный приоритет сохранён в URALBYTE-12, комментарий **100**,
longId `6acb645920b4f92791265df0`, 2026-10-11T10:26:33Z. Его не повторять.
Предыдущий delivery-комментарий туда **NOT_POSTED**: обе попытки HTTP 429.

После прежних отказов идемпотентное создание подтвердило **URALBYTE-13**:
`outcome=created`, creation key `chief-agent-runtime-spike-2026-10-11`.
Pure addition `URALBYTE-13 has-epic URALBYTE-3` после одного HTTP 429 подтверждена
receipt: `action=issueLinks.add`, `owner=URALBYTE-13`, `outcome=added`, `id=25`.
Create и link receipts возвращают availability unavailable / ai_access_unavailable:
это подтверждённые записи с закрытым содержимым, а не право читать скрытые данные.
Создание не повторять. Свежий whoami подтвердил qdinov и cloud org
bpf53toodvl9r97aasc2; AI access check всё время включён. Issue metadata read
по-прежнему HTTP 429; актуальные transition IDs/status прочитать не удалось,
статусы не угадывались.

[Delivery update](agent-runtime-spike-delivery-pending.md) с результатами и новым
пользовательским разрешением сохранён в **URALBYTE-13 comment 101**. Receipt:
`action=comments.add`, `owner=URALBYTE-13`, `outcome=added`, `id=101`,
availability unavailable / ai_access_unavailable. Это подтверждённая запись,
содержимое issue не раскрывалось. Последний normalized-context timeout получен
после публикации комментария и сохранён локально; отдельно в Tracker не дописан.
Handyman создал и attached Draft PR7; ключ задачи указан в PR. Обратная ссылка
из Tracker пока **NOT_POSTED**: comments.add HTTP 429 без receipt. Наличие PR
и зелёного checkpoint CI не завершает spike или human acceptance. Merge и
закрытие задачи не выполнялись; этот агент PR не менял. Надёжных полных интервалов активной работы по ролям нет, worklog
и стоимость задним числом не выдуманы.

## Передача для следующей итерации

Этот раздел позволяет возобновить работу без восстановления агентских логов.
Последнее основание: URALBYTE-13, Draft PR7, head checkpoint c908f98, утверждённый
ограниченный capture probe; общий итог **INCONCLUSIVE / NOT_DECIDED**.

1. Capture code завершён; независимый scoped Checker выдал 87 PASS, 12.43 с,
   Ruff/format PASS и standalone ZIP unauthorized 401 PASS. Полный Checker
   остаётся FAIL по внешним live gates; эти результаты не означают native PASS.
2. После проверок Handyman обновляет существующий Draft PR7 согласованным
   checkpoint. Финальные counts, SHA и CI привязываются к конкретной версии;
   593 PASS относится только к c908f98. Полный spike от этого не закрывается.
3. Для live продолжения нужен реально опубликованный endpoint и evidence
   native binding/ACK/callback по утверждённому probe. Сейчас endpoint отсутствует,
   bounded Cloud Functions preflight завершился 503/timeout. Это blocker внешнего
   шага, без доказанной причины; дополнительные бесконечные retries не планируются.
4. Временный switch @UralbyteDemoBot отдельно авторизован, но не выполнен.
   Перед переключением инженерная роль подтверждает готовность capture,
   единственного consumer и восстановления исходного транспорта. Только genuine
   результат владельца может закрыть соответствующий Telegram gate.
5. Финальные scoped counts получены; Steward подготавливает одну актуальную
   запись в URALBYTE-13 с обратной ссылкой PR7, точным CI checkpoint, 87 локальными
   PASS, внешними NOT_RUN и NOT_DECIDED. Её публикация требует подтверждённого
   receipt; comments100/101 не повторяются. CI будущего Handyman checkpoint
   не объявляется зелёным заранее.

После полного Checker PASS и Reviewer APPROVE Planner обновляет сравнение,
Steward сохраняет evidence-based recommendation, пользователь отдельно решает
вопрос миграции. Ни Draft PR, ни разрешение временного теста не разрешают
полную миграцию, постоянный production switch, merge или закрытие заказа.

Последняя итоговая запись с PR7 и scoped 87 PASS **NOT_POSTED**: свежий whoami
HTTP 429. Без подтверждённой организации mutating calls не выполнялись; retry
не запускался согласно ограничению auth gate. Готовый body сохраняет факты
87 PASS и CI старого c908f98, не предсказывая будущий head. Comments100/101
не повторять. Полный spike и задача остаются незавершёнными.
