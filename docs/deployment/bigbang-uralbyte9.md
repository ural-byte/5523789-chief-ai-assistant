# URALBYTE-9: проверка обновления bigbang

Статус: source gates, CI, развёртывание и recovery проверены. Настоящий Telegram
owner flow ожидается; обе записи сохранены, удаление не подтверждалось.
[Тикет](https://tracker.yandex.ru/URALBYTE-9) блокирует демонстрацию до повторной
проверки. Клиентская приёмка PENDING. Вердикты исходного MVP и URALBYTE-8 не являются
проверкой исправления URALBYTE-9.

2026-10-05 пользователь разрешил один дополнительный цикл. Свежие SOURCE Checker
PASS (292 + 68 проверок) и Reviewer APPROVE (24 собственные проверки) получены:
границы subject/narrator/selector проверены на том же замороженном source tree.
Настоящий owner confirm не выполнялся.

## Контроль до поставки

2026-10-04 17:59:01 UTC, текущая schema `0004`, executable `d39e998`:
две записи клиентского конфликта присутствуют, три факта и две сущности; один
task и одно approval. На этом снимке документов и chunks нет.
Содержимое записей не включается в публичные отчёты.

Read-only контрольные суммы фиксируются перед обновлением и повторно после него.
Миграция сама не должна удалять записи; только настоящий owner confirm выборочного
approval разрешает удаление выбранной устаревшей записи. Исходная зависшая операция
`29f20a30-4cb8-4be7-8ae2-dc5d88ed0ebe` восстанавливается без повторения AI/tools.

## Обязательные проверки поставки

1. Независимые Checker PASS и Reviewer APPROVE для замороженного source tree;
   CI exact commit и новый PR со ссылкой на URALBYTE-9.
2. Immutable image digest и OCI revision соответствуют проверенному коммиту.
   Context `yc-big-bang`, namespace `chief-ai-assistant`, прежние PVC и Telegram route.
3. Alembic `0005`, app 3/3 Ready, DB Ready, backend health и worker heartbeats.
   Контрольные суммы пользовательских данных совпадают после миграции.
4. Прежний whitespace-only final retired, единственный error recovery доставлен
   с фактическим Telegram ACK. AI/tool/domain actions исходной операции не повторены.
5. Настоящий вопрос владельца о руководителе Альфа → конфликт; выбор Петрова → точная
   карточка общего approval. Две записи сохраняются до нажатия владельцем кнопки.
6. Настоящий owner confirm → ровно выбранная устаревшая запись удалена, актуальная
   сохранена. Повторный вопрос возвращает одну актуальную версию; Checker сверяет
   incoming updates, audit и реальные ACK. Synthetic owner callback не используется.
7. Итоговая запись содержит observed outcomes и ограничения; общий клиентский
   вердикт назначает клиент. До завершения этой проверки BLOCKED не снимается.

## Наблюдавшаяся поставка 2026-10-05

[PR3](https://github.com/ural-byte/5523789-chief-ai-assistant/pull/3) открыт как draft
в `uralbyte-8-ux-data-controls`; merge не выполнялся. Проверенный и развёрнутый
source commit: `d8f463cb6baf7851abb60eac851640c635147b1f`. Оба CI успешны на нём:
[push](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37260891037),
[PR](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37260931433).

Immutable image:
`cr.yandex/crp5er6iam3h65oe4kdd/chief-ai-assistant@sha256:d4e42aac6c284d41dcd454c4dd02647779b78cdf3f0c8e6cc916ebd375cca27c`.
OCI revision совпадает с source commit; linux/amd64. Обновление выполнено reviewed
`deploy.py` после dry-run. Rollout успешен; новый app pod 3/3 Ready, ноль рестартов,
БД Ready, schema `0005`, backend health и обе worker heartbeats HTTP200.
PVC и прежний Telegram route сохранились. Hash deployed memory-resolution helper:
`f5294b25825ae5f8d444c8d1a20480971e10cca4b8233dc474a9d88a52973e07`.

Все девять групп контрольных сумм данных совпали до и после миграции; обе
конфликтующие записи и их 2 + 1 факта сохранены. Read-only проверка уже развёрнутого
helper подтверждает совместимость пары. Эти проверки не создают approval.

Прежний пробельный final переведён в failed после 155 исторических попыток,
без фиктивного ACK. Единственный recovery final доставлен с первой попытки:
фактический Telegram ACK `2026-10-05 03:54:19.759212 UTC`. Операция имеет terminal
error и delivered для конечного сообщения. Исходные три AI calls, один tool
invocation и job с одной попыткой сохранились без повторения доменных действий.
Независимый Checker подтвердил deployment/recovery и сохранность данных;
результат зафиксирован в комментарии 64 тикета.

[Обезличенные метрики](../measurements/2026-10-05-uralbyte9-deployment.json).
Пункты 1–4 списка поставки выполнены; пункты 5–6 требуют настоящих сообщений
и подтверждения владельцем в Telegram. Карточку необходимо проверить до confirm:
после успешного удаления её приватный preview очищается. Reset и synthetic callback
не выполнялись. Общий клиентский вердикт PENDING, демонстрация BLOCKED.
