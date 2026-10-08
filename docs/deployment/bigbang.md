# Проверка deployment bigbang, 2026-10-04

Executable source: `181dbc5b378bcdd0cb48c9d33682860e2fc2ede2`.
Image: `cr.yandex/crp5er6iam3h65oe4kdd/chief-ai-assistant@sha256:944da42aa52f70533b354f8e2ea9aeb875e52a0d839759936fb6dedbec8b650f`.
Deployment/configuration source: `c53a55c389ec59463456b75aafe92c7149446854`.
Последующие изменения документации не меняют executable image.
[PR](https://github.com/ural-byte/5523789-chief-ai-assistant/pull/1),
[CI source push](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37204303797),
[CI source PR](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37204306996).

## Наблюдавшиеся результаты

- Context `yc-big-bang`, namespace `chief-ai-assistant`, node zone `ru-central1-d`.
  App 3/3 Ready, DB 1/1 Ready; backend health и оба heartbeat прошли.
- PostgreSQL Alembic revision `0003`, extension `vector`. PVC `documents` 2Gi,
  `postgres-db-0` 10Gi, оба Bound/RWO/yc-network-hdd. При обновлениях claims сохранялись.
- Настоящий AI probe `928afb96-9f66-4090-9845-1ada654b5ee5` прошёл native roundtrip,
  doc/query embeddings 256. Все четыре сценария измерены новым executable image:
  [14 HTTP attempts / 6 operations](../measurements/2026-10-04-bigbang.json), все done;
  фактическое потребление и 6,0141918 RUB сверены независимым Checker с ai_calls БД.
- Обычный DNS Telegram TCP443 недоступен из pod. Явная [route overlay](../../deploy/recommended/kubernetes/README.md)
  проверена через CA/hostname TLS1.3 и настоящий getMe HTTP200. После её применения
  long polling сохранил checkpoint, Telegram подтвердил sendMessage; 16 сообщений done
  уже при первом контрольном чтении. IP закреплён временно, причина исходного маршрута не установлена.
- Memory original SHA256, PDF file SHA256/status и immutable approval payload SHA256
  совпали с markers после app recreate. PostgreSQL отдельно успешно перезапущен.
- Реальный владелец подтвердил approval `189f530d-8d25-4564-8553-df2c9b93351d` в Telegram:
  `approved_at=2026-10-04T13:20:37.686336Z`, `executed_at=2026-10-04T13:20:37.687780Z`,
  status `simulated`. Эти timestamps/status сохранились после PostgreSQL restart.
  Синтетический callback не использовался; календарь не подключён.

## Просроченное напоминание

Проверка прошла с настоящим временем и Telegram API: task
`fe64dbbf-04b4-4c9e-80b7-ba5917a8cbb9`, operation
`eb9093af-d58d-44af-ac30-0bbab79829ed`, deadline `2026-10-04T13:30:08Z`.
App pod уже отсутствовал, задача оставалась pending при чтении `13:29:00.445Z`;
после deadline в `13:30:36.578Z` при отсутствующем app pod всё ещё pending.
После возобновления task стал notified. В `13:32:57.219Z` единственная outbox запись
`task:fe64dbbf-04b4-4c9e-80b7-ba5917a8cbb9:reminder` была done, attempts=1;
Telegram API принял текст «Отправлено с задержкой 71 сек. после срока».
Ранние двухминутные fixtures были scheduler-notified во время termination grace
и не выдаются за эту проверку. Hash/state проверки не изменяли сроки или содержимое БД.

## Поиск после перезапуска

После app и PostgreSQL restart выполнены новые запросы с настоящим Yandex API:
поиск исходной памяти — operation `fbf1f9ab-8831-4317-9c05-c0cbe8e3d45c`,
вопрос по исходному PDF — `cfbb3edc-bf35-4d6c-97f0-cf7f7902114c`.
Оба done; инструмент вернул исходный memory ID, PDF ответ опирается на исходный
документ и страницу 1. Метрики этих дополнительных проверок сохранены отдельно
от измеренного демонстрационного набора. Artifact в persistent volume:
`/srv/data/documents/benchmarks/post-restart.json`.

## Приёмка

Независимые итоговые инженерные вердикты: **Checker PASS, Reviewer APPROVE**. Проверены реальные postrestart
ответы и Telegram outbox done, оригинальные источники, AI usage/pricing и уникальная
доставка просроченного напоминания; source/config и operational limit описаны выше.

Инженерные проверки не заменяют клиентский вердикт. Клиентская приёмка пока не проведена;
ACCEPT / ACCEPT WITH CHANGES / REJECT не присвоен. Blocking/non-blocking замечания
фиксируются в [acceptance](../acceptance.md) и URALBYTE-7 после ответа владельца.
