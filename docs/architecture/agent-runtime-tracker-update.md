# Подготовленная запись в Трекер: изменение приоритета

Статус: **PARTIALLY_POSTED**, 2026-10-11. Приоритет и согласованный spike
сохранены в комментарии **100** URALBYTE-12 (longId 6acb645920b4f92791265df0,
2026-10-11T10:26:33Z). Повторно этот комментарий не публиковать. Отдельный тикет
и комментарий в эпике пока не подтверждены: CLI возвращает HTTP 429.
Создание тикета повторять только с тем же creation key
chief-agent-runtime-spike-2026-10-11; до verified task key PR запрещён.
Ниже сохранён исходный текст планирования; актуальное состояние backend adapter
описано в agent-runtime-spike-adapter.md.

## Комментарий в существующий эпик и URALBYTE-12

Steward: по прямому запросу пользователя от 2026-10-11 меняем следующий этап
развития ural-byte/5523789-chief-ai-assistant. Перед дальнейшим большим refactor
и продолжением C26 проводим отдельный architecture spike Yandex Agent Atelier.

Цель: Telegram → Agent Atelier → MCP/tools → backend. Atelier отвечает за
conversational context, reasoning, tool selection, multi-step interaction и
skills/instructions. Backend сохраняет данные, permissions/owner isolation,
idempotency, scheduler, audit и approvals. Destructive/external actions физически
не исполняются без valid backend approval.

POC включает только save_memory/search_memory/list_memory/delete_memory и
create_task/list_tasks; delete_memory подготавливает immutable approval, а
исполнение выполняется genuine owner callback по конкретному approval_id.
Проверяем memory-management/task-management/destructive-actions как skills и
instructions, отдельно фиксируя поддержку Skills API.

Обязателен живой Telegram flow: сохранены Саша и Лёша как руководители команды
Сьерра → показан конфликт → «Лёша — правильный вариант» → готовая backend
approval-карточка → настоящий owner confirm → свежий retrieval только Лёши.
Новый backend conversational workflow для этого кейса недопустим.

Planner после POC сравнивает A Atelier/B Responses API/C текущий runtime по
качеству диалога, tools/skills, Telegram, control/safety, observability, lock-in,
complexity/maintenance, стоимости, доступности из РФ и скорости разработки.
Steward фиксирует MIGRATE TO AGENT ATELIER / USE RESPONSES API / KEEP CURRENT
RUNTIME с доказательствами. A имеет приоритет при PASS; B проверяется при
ограничениях A; C допустим только при объективной непригодности обоих.
При неполных доказательствах рекомендация NOT_DECIDED. После отчёта ждать
решения пользователя, не запускать миграцию автоматически.

Текущая реализация и прежние результаты/незавершённая приёмка сохраняются.
Полная миграция, все tools, Gmail/Calendar/web search и production deployment
в scope не входят. Plan: docs/architecture/agent-runtime-spike.md.
POC NOT_STARTED; живой gate NOT_RUN; рекомендация NOT_DECIDED.

## Самостоятельный тикет spike (создать только при отсутствии дубля)

Название: Architecture spike: Agent Atelier runtime, MCP tools и Telegram approval.
Очередь: URALBYTE. Родитель: существующий проверенный эпик URALBYTE-3.
Точный ключ нового тикета пока неизвестен; не объявлять его созданным.
Описание и критерии: полный план из agent-runtime-spike.md.
Связать с URALBYTE-12 как новый приоритет до большого refactor, не закрывать
URALBYTE-12 и не изменять прежние клиентские вердикты.
До реализации Handyman фиксирует отдельный workspace и точный base commit,
Planner/Plan Challenger согласуют инженерный план. Проверки POC — Checker и
Reviewer, затем Planner/Steward comparison и отдельное решение пользователя.
