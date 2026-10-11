# Capture-only MCP probe

Локальный диагностический kit: standalone stdlib Cloud Function, private
prepare/run script и тесты без БД, Telegram и production `.env`. Existing backend
binding gate не меняется. Полная Telegram приёмка остаётся NOT_RUN; scoped checks
не означают PASS всего spike или разрешение переключить бота.

Endpoint экспортирует только synthetic list_tasks с limit enum 7/11, без dataset
или настоящих handlers. POST JSON MCP subset поддерживает initialize,
notifications/initialized, tools/list/tools/call. Auth обязателен для discovery;
tools/call дополнительно проверяет signed E1/E2. Их expiry ограничен 30 минутами.
Origin запрещён, body ограничен 16 KiB, обработка tool call задерживается на 400 ms
(максимум 500 ms) для измеримого overlap. Missing/bad binding — 403, swapped7/E2 и
11/E1 correlation FAIL с HTTP 403, whole endpoint после expiry — HTTP 410.

Независимая private expected map Q1/F1→E1, Q2/F2→E2 строится из canonical
arguments; header не определяет fingerprint. Endpoint пишет NOT_VERIFIED даже
для правильной пары: originating Responses request надо доказать отдельно.
Runner сопоставляет successful provider output fingerprints с Q1/Q2, требует
ровно два distinguishable matching capture calls, оба captured swapped FAIL,
успешные negative checks и фактическое пересечение capture intervals внутри
интервалов соответствующих provider requests. Missing/ambiguous correlation,
нет overlap или неполный provider output сохраняют NOT_VERIFIED. Synthetic
transport PASS никогда не доказывает native Telegram identity/callback/approval.

## Private подготовка

```sh
python -m scripts.spike_mcp_probe prepare --directory /private/tmp/chief-capture-<fresh-name> --ttl 1800
```

Directory создаётся с правами 0700; private.json, expected.json и function.zip — с правами 0600,
existing файлы не перезаписываются. ZIP содержит ровно probe.py и
_probe_private.json с fresh dedicated probe bearer/key/nonces/expiry. Private
config находится в upload package; credentials не передаются в CLI argv,
function environment или deployment JSON. ZIP нельзя публиковать, прикладывать
к PR или копировать в общую директорию. Function не читает environment/context
IAM token и не получает runtime service account, VPC, DB, Lockbox или Telegram.

Скрипт выводит только run UUID и expires_at. При ошибке выводит exception class,
а не exception text. Никакие ресурсы автоматически не создаются.

## Hosting handoff

Root выполняет только отдельную диагностическую публикацию после scoped
independent Checker. Overall Checker FAIL/live NOT_RUN и Reviewer полного spike
не подменяются этим разрешением. На 2026-10-11 runtime hosting preflight дал 503,
затем bounded timeout; ресурсы не создавались, deployment/provider MCP probes
остаются NOT_RUN. Повторную инфраструктурную диагностику kit сам не запускает.

Рецепт для доступного hosting, без secrets в argv:

1. Создать label-scoped temporary function, отдельную capture loggroup и gateway.
2. Загрузить private ZIP как source-path с entrypoint probe.handler, проверенным
   Python runtime (код совместим с Python 3.12), memory 128 MB, timeout 3 s,
   concurrency 2, async retries 0. Function service account/network/mounts не задавать;
   metadata endpoints отключить. Указать dedicated loggroup: stdout нужен для
   durable safe capture. Не включать debug или печать upload package.
3. Для Gateway→Function создать отдельный service account с единственным
   serverless.functions.invoker на этой function. Function не публиковать как
   unauthenticated direct endpoint. Gateway даёт временный HTTPS /mcp и только
   POST/GET routes; GET handler возвращает 405. Gateway access logging выключить
   через --no-logging, без network/broad IAM.
4. В OpenAPI integration для обеих routes задать type cloud_functions,
   function_id и service_account_id только этих временных ресурсов. Никаких
   credentials в spec, metadata, labels или descriptions. Данные формата event
   соответствуют [Python handler contract](https://yandex.cloud/en/docs/functions/lang/python/handler).
5. Явно сохранить IDs/expiry/cleanup в private resource record. Ограничить ingress
   quota/rate и время существования ресурсов. Stateless noDB handler не обещает
   глобальный call counter across cold starts: bounded body/processing/expiry
   сочетаются с конечным runner budget и инфраструктурным лимитом ingress.

Пример non-secret version arguments: --source-path <private-dir>/function.zip
--entrypoint probe.handler --runtime <verified-runtime> --memory 128MB
--execution-timeout 3 s --concurrency 2 --async-max-retries 0 --log-group-id <temporary-id>.
CLI output сохранять приватно и проецировать только metadata IDs/status;
не выводить целые responses. Run не меняет production polling/webhook.

## Provider run и capture collection

Provider API key Root отдельно помещает в private regular file с правами 0600, без токена
в argv/чате. Скрипт не читает `.env`, dotenv, yc profile или произвольные secrets.
URL допускает только HTTPS temporary *.apigw.yandexcloud.net/mcp без userinfo,
query/fragment. Redirects отключены для защиты credentials.

```sh
python -m scripts.spike_mcp_probe run --directory <private-dir> --url <https-gateway>/mcp --model gpt://<folder>/<verified-model> --provider-key-file <private-key-file>
```

Сначала четыре direct negative calls; несовпадение status и фиксированного error
останавливает provider этап. Затем ровно два одновременных official Responses
requests: Q1 limit 7/E1, Q2 limit 11/E2, allowed_tools list_tasks, store false,
max_output_tokens 256, max_tool_calls 2, timeout 15 s, без client tool loop/повторных retries.
Tool credentials находятся только в request-scoped tools.mcp.headers. Эта
кандидатная capability выбранного provider ещё требует живой проверки.

provider-evidence.json содержит только validated IDs/status/latency/usage,
output type/tool enums и independently validated fingerprints. Top-level tools,
headers, raw arguments, user/model text, error bodies и exception strings не
выводятся и не сохраняются. Unknown IDs/types становятся null/unsupported.

Функция сохраняет safe JSON stdout в provider loggroup. Это durable capture,
а не global buffer; cold starts не теряют уже записанное evidence. Root читает
только logs временной function с bounded limit/time window в private файл с правами 0600.
Не печатать полный logging response: он может содержать инфраструктурные записи.
Потом:

```sh
python -m scripts.spike_mcp_probe summarize --directory <private-dir> --captures-file <private-logging-json>
```

Поддержан JSON array entries либо объект с entries; message парсится как JSON.
Unknown records/keys/values отклоняются. На выходе только fixed allowlist capture
и correlation verdict. Artifact capture-evidence.json тоже с правами 0600. Redaction
включает arbitrary header/method/tool/key/RPC ID injections; RPC ID не входит
в capture. Нет admin endpoint и metadata не возвращается модели.

## Завершение

Root удаляет gateway/function/version, temporary invoker account и loggroup после
сохранения redacted evidence; удаляет private archive/config/key/log input.
Expiry блокирует endpoint, но не заменяет удаление ресурсов или rollback бота.
Бот в capture этапе не переключается. Native binding остаётся NOT_VERIFIED,
SPIKE_BINDING_VERIFIED=false. Решение о следующем live bridge требует отдельного
handoff, не скрытого расширения этого kit.

Локальная проверка:

```sh
python -m pytest tests/spike_atelier/test_probe.py -q
ruff check app/spike_atelier/probe.py scripts/spike_mcp_probe.py tests/spike_atelier/test_probe.py
```

Tests: request-scoped E1/E2, swapped FAIL, auth/expiry/body/schema, arbitrary-secret
redaction, private permissions/package, parallel processing и independent
provider correlation. Provider/network в тестах synthetic; это не live proof.
