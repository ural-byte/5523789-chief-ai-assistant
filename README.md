# Персональный AI-помощник в Telegram

Каркас Python/FastAPI/PostgreSQL с pgvector, сменным AI adapter и очередями в БД. Telegram использует long polling. Agent runtime сохраняет историю и результаты инструментов, контролирует операции и стоимость AI. Поручения, approvals, память и PDF подключаются через доменные обработчики следующими изменениями; пока незарегистрированные возможности не предлагаются модели.

## Локальный запуск

Требуются Docker и Compose v2. Скопируйте `.env.example` в локальный `.env`, задайте случайный `SERVICE_TOKEN`, пароль PostgreSQL и согласуйте его с `DATABASE_URL`. Укажите разрешённый Telegram ID, Telegram-токен, `AI_API_KEY` и `AI_FOLDER_ID`. Секреты передаются окружением и не входят в Git.

```sh
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

Backend применяет Alembic migrations перед запуском. Четыре сервиса имеют restart policy и health checks. PostgreSQL и файлы имеют постоянные volumes. Без AI/Telegram credentials каркас поднимается для проверки инфраструктуры, но не является доступным демонстрационным ботом. По умолчанию часовой пояс `Europe/Moscow`; относительные даты привязаны к времени исходного Telegram-сообщения, чтобы очередь после простоя не сдвигала намерение пользователя.

## AI и стоимость

Generation: `gpt://<folder>/deepseek-v4-flash`, Yandex OpenAI-compatible API `https://ai.api.cloud.yandex.net/v1`, заголовки `Api-Key` и `OpenAI-Project`. Embeddings: `emb://<folder>/text-embeddings-v2-doc/` и `text-embeddings-v2-query/`, запрашивается 256 измерений. Невалидный/нечисловой вектор отклоняется.

`TOOL_PROTOCOL=native` включает native tool calling с сохранением `tool_call_id`. `TOOL_PROTOCOL=json` явно выбирает строгий JSON-протокол `final{text}` либо `tool{name,arguments}`. Режим не переключается автоматически при ошибках сети, квот или авторизации. Локальные тесты используют тестовый transport; production не имеет поддельных ответов.

После настройки окружения и migrations выполните безопасную проверку:

```sh
docker compose exec backend python -m scripts.probe_ai
```

Probe выполняет безвредный echo tool roundtrip и doc/query embeddings, сохраняет метрики в БД и выводит статус с идентификатором операции без запросов/секретов. Если native tools действительно не поддерживаются, зафиксируйте ошибку и явно выберите JSON mode, затем повторите проверку. Live proof пока **pending**: AI credentials отсутствуют; поддержка конкретной модели реальным запросом ещё не подтверждена.

Каждая HTTP-попытка AI имеет собственную строку `ai_calls`: provider/model/type/scenario, nullable фактические токены, numeric usage details, latency, status/error, логический call ID, attempt, стоимость/полнота/валюта/версия и snapshot ставок. Начатая до сбоя попытка остаётся с неизвестным исходом. Retries считаются отдельными попытками; каждый generation request расходует общий бюджет операции. Для безопасности многоязычного контекста используется консервативная оценка по UTF-8 bytes полной сериализации запроса с запасом. Лимиты: суммарный вход 16 000, выход 2 000 на вызов, максимум пять индивидуальных вызовов инструментов, включая некорректные. Перед финальным ответом инструменты отключаются.

Ставки находятся в `config/pricing.json`, а не в доменной логике. Снимок на 2026-10-04: DeepSeek вход 0,30 ₽/1000, cache 0,075 ₽/1000, выход 0,50 ₽/1000; embeddings 0,0101 ₽/1000. Cache является частью входа, reasoning — частью выхода; результаты пользовательских функций оплачиваются как обычный вход. Embedding prompt/total учитывается один раз. Если применимая метрика неизвестна, стоимость отмечается неполной; отсутствие usage не превращается в нулевое потребление. [Тарифы Yandex](https://aistudio.yandex.ru/ru/docs/ai-studio/pricing), [embeddings](https://aistudio.yandex.ru/ru/docs/ai-studio/concepts/embeddings).

`GET /internal/operations/{id}/usage` возвращает сумму известной стоимости, количество неполных вызовов и известные токены с количеством неизвестных значений. Результаты реальных четырёх сценариев пока pending; их таблица с моделями/токенами/стоимостью будет заполнена после подключения доменов и настоящего API. Для PDF измеряются отдельно индексация и вопрос. Тестовые показатели не являются экономикой живой демонстрации.

## Внутренние контракты

Все `/internal/*` требуют `Authorization: Bearer <SERVICE_TOKEN>`. `POST /internal/updates` сохраняет update, операцию и job одной транзакцией до ответа; `update_id` уникален. Чужие updates игнорируются. Long polling продвигает сохранённый checkpoint только после durable ingress. Callback и document metadata также сохраняются. Данные и история привязаны к владельцу; смена разрешённого ID открывает отдельную историю, очереди старого владельца не обрабатываются.

`Registry` предоставляет только реально зарегистрированные `create_task`, `prepare_meeting`, `save_memory`, `search_memory`, `search_document`. Обработчик объявляет Pydantic arguments (рекомендуется `extra=forbid`), `prepare(ctx,args)` для AI/IO вне транзакции и `apply(session,ctx,args,prepared)` для атомарного изменения домена и сохранения результата invocation. Context содержит владельца, chat/operation IDs, reference UTC, timezone, idempotency key и source update. `ToolResult` имеет `status` (`ok`, `needs_clarification`, `error`), `data`, `user_message`, `buttons`, `sources`. Для результата с approval buttons обязательный `user_message` содержит каноническое описание из immutable payload; пользователю отправляется оно, а не пересказ модели. Callback, uploads и виды фоновых jobs подключаются отдельными hooks.

Полный результат сохраняется в invocation; модель получает сокращённую проекцию, рассчитанную под оставшийся бюджет финального запроса. Все пять источников и поля document ID/name/page/chunk ID сохраняются; сокращаются только excerpts и текстовые данные, с флагом truncation. Необходимые метаданные, которые не помещаются, приводят к понятной ошибке лимита. После рестарта завершённый invocation не выполняет доменную запись повторно.

Jobs/outbox используют PostgreSQL `SKIP LOCKED`, lease 120 секунд и уникальные ключи. Background worker обновляет lease каждые 15 секунд; stale token не может фиксировать результат. Операции одного владельца обрабатываются последовательно; DB-транзакция не удерживается во время AI. Outbox разбивает plain text по 4000 UTF-16 units и повторяет отправку после ошибок. Если Telegram принял сообщение, а процесс упал до ack, возможна повторная отправка. Это окно внешней доставки не даёт повторных доменных действий. Логи содержат job IDs и классы ошибок; тексты provider exceptions и URL Telegram с токеном не выводятся.

## Проверки и развёртывание

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
TEST_DATABASE_URL=postgresql+psycopg://assistant:assistant@localhost:5432/assistant_test .venv/bin/pytest
.venv/bin/ruff check .
```

Тесты требуют отдельную настоящую PostgreSQL/pgvector БД с суффиксом `_test` и очищают её. CI проверяет lint и тесты с pgvector service. Lock-файлы закрепляют прямые и транзитивные зависимости.

На одном VPS в РФ установите Docker/Compose, получите checkout нужной версии, создайте `.env` непосредственно на сервере и выполните локальные команды запуска. Backend опубликован только на loopback. Для обновления получите новую версию checkout, выполните `docker compose up -d --build` и проверьте `compose ps`, health/probe и демонстрационные сценарии. `docker compose down` сохраняет volumes; `down -v` удаляет данные и не используется при обновлении. Смена пароля существующего PostgreSQL volume требует отдельного изменения роли БД; одно редактирование `.env` пароль в существующей БД не меняет.

VPS deployment и живые сценарии остаются pending до предоставления инфраструктуры/локальных credentials. Инженерная готовность требует реального доступного бота, напоминания и проверки сохранности данных после рестарта.
