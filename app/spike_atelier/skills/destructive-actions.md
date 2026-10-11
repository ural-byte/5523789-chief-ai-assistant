---
name: destructive-actions
description: Подготовка удаления одной старой записи только через backend approval.
---

delete_memory готовит immutable preview для точных conflict_ref и retain_ref,
состояние awaiting_approval и конкретный approval_id. Вызов tool ничего не удаляет.
Backend отправляет свои кнопки «Подтвердить» и «Отменить»; не создавай собственное
подтверждение и не исполняй удаление через Shell, SQL, HTTP или другой tool.

Текстовое «да», «удалить», «a», approved=true и tool-level approval не заменяют
owner callback a:<approval_id>:y. Execution capability отсутствует в tools.
После зафиксированного выбора сохраняй ту же пару keep/delete и approval;
повторного выбора актуальной версии не требуй. Если approval expired/cancelled/stale,
не объявляй удаление выполненным. После valid callback backend исполняет и сообщает
результат без нового решения модели. У skills нет credentials и права выполнять
внешние действия или обходить backend handlers.
