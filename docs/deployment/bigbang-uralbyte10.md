# Bigbang: URALBYTE-10

Задача [URALBYTE-10](https://tracker.yandex.ru/URALBYTE-10),
[PR4](https://github.com/ural-byte/5523789-chief-ai-assistant/pull/4),
stacked base `uralbyte-9-memory-conflict-terminal`; merge не выполнялся.

Проверенный source commit: `82d346de0c579c988408ec9f348abd9b494a2564`.
Независимые Checker PASS (325 тестов, 11 дополнительных, 4 настоящих Yandex
сценария) и Reviewer APPROVE (два собственных adversarial теста).
Все 40 source/test SHA256 совпали до и после независимых проверок.
CI exact source head: [push PASS](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37281755540),
[PR PASS](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37281798452).

## Образ и окружение

Образ linux/amd64 опубликован по immutable ссылке:

```text
cr.yandex/crp5er6iam3h65oe4kdd/chief-ai-assistant@sha256:1e3cdea968ac920e6135d89386c6c645e97906ad7b24cd213f9530ae20b62b07
```

AMD64 child digest `sha256:26c2c8b8b60d8a0423394735cb59773d1b95e7fd7799330a13bba62fec6af6bd`.
OCI revision совпадает с source commit; секреты исключены из build context.
Контекст `yc-big-bang`, namespace `chief-ai-assistant`, три процесса одного
Deployment и PostgreSQL. Использован существующий recommended deploy helper,
dry-run прошёл. HostAliases Telegram сохранены; TLS проверяется штатно.
Миграции и зависимости в этой доработке не менялись, schema `0005`.

## Проверка поставки

Read-only snapshot перед заменой: 2026-10-05 08:11:46 UTC. В этот момент память,
факты, задачи и документы пользователя пусты; два approvals присутствуют.
Это фактическое состояние после предыдущих пользовательских операций, а не reset
в рамках развёртывания URALBYTE-10.

Новый pod `assistant-6d44cf7ccf-jpnvn` запустился 08:13:22 UTC, все три контейнера
Ready, ноль рестартов; PostgreSQL Ready. Rollout завершился. Первый ограниченный
45-секундный wait истёк во время штатного запуска; повторная проверка подтвердила
готовность. Init migration завершился exit0, схема осталась `0005`.
Backend health и оба worker heartbeat endpoints вернули HTTP200. Шесть изменённых
production modules имеют точные SHA256 из независимо проверенного manifest.

Read-only snapshot после замены: 08:14:39 UTC. Все девять групп совпали с исходными
хэшами: память и её доменные поля, факты, сущности, документы, chunks, файлы, задачи,
immutable approvals. Состояние пользователя не изменено поставкой. Прежние PVC:
documents `pvc-20c68842-e5ec-476f-b161-047159f978de` (2Gi), PostgreSQL
`pvc-148f28c3-fe05-495c-b232-f2fd0dd5b430` (10Gi), оба Bound.
[Обезличенные результаты](../measurements/2026-10-05-uralbyte10-deployment.json).

Настоящий owner Telegram save → итоговый ответ LLM → ACK → последующий поиск
ещё ожидается. Изолированные Yandex тесты не считаются этой проверкой.
Клиентский вердикт PENDING и блокировка демонстрации по URALBYTE-9 не меняются.
