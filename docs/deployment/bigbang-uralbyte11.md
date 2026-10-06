# Bigbang: URALBYTE-11

Обновление выполнено 6 октября 2026, 09:39 Asia/Yekaterinburg (04:39 UTC).
Задача [URALBYTE-11](https://tracker.yandex.ru/URALBYTE-11),
[PR5](https://github.com/ural-byte/5523789-chief-ai-assistant/pull/5),
stacked base `codex/memory-intent-llm`; merge не выполнялся.

Проверенный executable source: `66e53cd189a20e02664a185aa7014023ea4a1b67`.
Checker PASS: 613 общих тестов, 109 допустимых независимых risk cases,
четыре свежих реальных Yandex native/JSON memory/PDF сценария. Reviewer APPROVE;
прежний multi-tool обход закрыт собственным воспроизведением Reviewer.
50 hashes source manifest совпали до и после независимых проверок.

CI exact source: [push SUCCESS](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37364055616)
и [PR SUCCESS](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37364100931),
третья попытка. Две первые попытки не получили hosted runner и не выполнили
ни одного шага. Это отдельные инфраструктурные исходы, не провал тестов и не PASS.
Повторы выполнены без изменения кода или workflow после восстановления GitHub.

## Образ и миграция

Опубликован и установлен linux/amd64 образ:

```text
cr.yandex/crp5er6iam3h65oe4kdd/chief-ai-assistant@sha256:da4733743bb269a01460ca60f8e7a4e6f751d88a6a7bdd4651a8c58b194fca73
```

AMD64 child digest `sha256:06c2ec9c1126d3602fcf2a7f8ed1a693f08bb9aaa335cca6d0caa78910580bed`.
Build provenance revision совпадает с executable source; build context исключает секреты.
Отдельный read-only запрос Checker к OCI config не завершился в ограниченное
время: config label UNVERIFIED, build provenance не подменяет эту проверку.
Recommended deploy helper: dry-run PASS, затем штатный Recreate в `yc-big-bang`,
namespace `chief-ai-assistant`. Старый pod остановился до запуска нового;
одновременные Telegram pollers не запускались. HostAliases Telegram и штатная
проверка TLS сохранены.

Новый pod `assistant-55cbf54c9b-nlb89` создан 04:38:50 UTC, три контейнера Ready,
ноль рестартов. Init `migrate` завершился exit0. Additive nullable миграция
обновила схему `0005 → 0006`; существующие данные не очищались.

## Проверка поставки

Backend health и оба worker heartbeat endpoints HTTP200. Хэши 11 production
modules совпали с проверенным source manifest. PostgreSQL Ready; один application
pod с backend, Telegram и background worker.

Read-only baseline перед заменой: 04:37:48 UTC; после: 04:39:52 UTC.
Все девять групп совпали: память и доменные поля, факты, сущности, документы,
chunks, файлы, задачи и immutable approvals. Сохранены три записи памяти,
три факта, две сущности и три approvals. Обе исходные записи Сьерра/Лёша/Саша
присутствовали перед обновлением. Reset, повторное сохранение Саши и synthetic
production callbacks не выполнялись.

Прежние PVC Bound: documents `pvc-20c68842-e5ec-476f-b161-047159f978de` (2Gi),
PostgreSQL `pvc-148f28c3-fe05-495c-b232-f2fd0dd5b430` (10Gi).
[Обезличенные результаты](../measurements/2026-10-06-uralbyte11-deployment.json).
Независимый Checker поставки: **PASS**. Проверены все 33 deployed app/migrations
файла и 50 published source hashes, точные image digests, миграция, три health
endpoints, heartbeat, один Telegram worker, PVC и сохранность девяти групп данных.
Настоящий owner flow в эту read-only проверку не входит.

## Оставшаяся клиентская проверка

Настоящий owner Telegram flow ещё **PENDING**: вопрос о руководителе Сьерры →
«Оставь Лёшу, другую версию удали» → точная необратимая карточка → настоящий
owner confirm → повторный вопрос только Лёша, без UUID/диагностических блоков.
Pre-confirm evidence фиксируется до очистки ссылок при confirm. До этого owner
criterion UNVERIFIED, клиентский вердикт PENDING и demo BLOCKED. Старые URALBYTE-9/10
клиентские статусы не закрываются автоматически.

Историческая причина C4 JSON pair0 остаётся UNKNOWN; потерянные факты не
восстанавливаются по хэшам. Двенадцать дополнительных JSON fixtures, которые
вызвали инструмент при пустой схеме, не считаются проверенными final paths:
backend вернул конечную ошибку без удаления и false completion. Source guards
и ограничение 16 000 не ослаблялись.
