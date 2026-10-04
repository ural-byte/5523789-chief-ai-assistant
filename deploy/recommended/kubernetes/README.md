# Recommended: Kubernetes bigbang

Утверждённый deployment target: context `yc-big-bang`, namespace `chief-ai-assistant`,
Yandex Managed Kubernetes, зона `ru-central1-d`. Docker Compose остаётся локальным запуском.
Манифесты рассчитаны на прототип: один PostgreSQL StatefulSet и один app pod с отдельными
контейнерами backend, Telegram и background. `Recreate` исключает штатный overlap pollers.
Это также позволяет backend/background совместно использовать один RWO documents PVC.
Workers обращаются к backend через localhost, поэтому readiness всего pod не блокирует
первую heartbeat. Для HA потребуются отдельный дизайн хранения и coordination pollers.

Хранилище: 10 GiB PostgreSQL и 2 GiB PDF, CSI `yc-network-hdd`, WaitForFirstConsumer.
Requests приложения+БД: 350m CPU, 736 MiB RAM; migration init 100m/128MiB.
Проверьте доступность ресурсов и StorageClass в своём кластере перед запуском.
Backend имеет только ClusterIP; ingress и публичный HTTP-доступ не нужны для long polling.
Secrets не входят в манифесты, Git или image. Init ждёт соединение БД (60 попыток,
connect timeout 5 секунд, пауза 2 секунды) и применяет Alembic до старта всех workers.
PostgreSQL image закреплён digest; app image передаётся только по digest.

## Запуск с чистого checkout

Требуются kubectl, доступ к указанному context, Docker Buildx, Python 3.12 и registry,
из которого node service account имеет право pull. Один бот должен иметь один poller;
остановите локальный Compose telegram перед deployment.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
cp .env.example .env
chmod 600 .env
# Заполните .env локальным редактором: Telegram, разрешённый ID, AI, SERVICE_TOKEN,
# POSTGRES_PASSWORD. Скрипт сам строит DATABASE_URL для Service db.
kubectl --context yc-big-bang get nodes
kubectl --context yc-big-bang get storageclass
REVISION=$(git rev-parse HEAD)
IMAGE="cr.yandex/<registry-id>/chief-ai-assistant:$REVISION"
docker buildx build --platform linux/amd64 --push --tag "$IMAGE" .
docker buildx imagetools inspect "$IMAGE"
# Возьмите sha256 digest из вывода, не mutable tag:
.venv/bin/python deploy/recommended/kubernetes/deploy.py \
  --context yc-big-bang --image 'cr.yandex/<registry-id>/chief-ai-assistant@sha256:<digest>' \
  --env-file .env --dry-run
.venv/bin/python deploy/recommended/kubernetes/deploy.py \
  --context yc-big-bang --image 'cr.yandex/<registry-id>/chief-ai-assistant@sha256:<digest>' \
  --env-file .env
kubectl --context yc-big-bang -n chief-ai-assistant rollout status statefulset/db --timeout=300s
kubectl --context yc-big-bang -n chief-ai-assistant rollout status deployment/assistant --timeout=300s
kubectl --context yc-big-bang -n chief-ai-assistant get pods,pvc
kubectl --context yc-big-bang -n chief-ai-assistant exec deploy/assistant -c backend -- \
  python -m scripts.probe_ai
kubectl --context yc-big-bang -n chief-ai-assistant exec deploy/assistant -c backend -- \
  python -m scripts.benchmark --output /srv/data/documents/benchmarks/demo --revision "$REVISION"
```

`--dry-run` только рендерит и делает client dry-run; он не отправляет credentials.
Обычный deploy проверяет владение namespace/Secret, передаёт Secret на stdin без вывода,
выполняет server dry-run манифестов и применяет только эти ресурсы. Secret хранится в
Kubernetes; ограничивайте права доступа к нему обычными средствами кластера.
`kubectl kustomize deploy/recommended/kubernetes` позволяет посмотреть specs без секретов.
`chief-ai-assistant:local` — placeholder, заменяется deploy.py на предоставленный digest.

Проверить HTTP без публичной экспозиции:

```sh
kubectl --context yc-big-bang -n chief-ai-assistant port-forward service/backend 8000:8000
# В другом терминале:
curl --fail http://127.0.0.1:8000/health
```

## Обновление и проверка после рестарта

Сохраните PostgreSQL backup и документы перед миграциями:

```sh
kubectl --context yc-big-bang -n chief-ai-assistant exec db-0 -- \
  pg_dump -U assistant -d assistant -Fc > assistant.dump
kubectl --context yc-big-bang -n chief-ai-assistant exec deploy/assistant -c backend -- \
  tar -C /srv/data -czf - documents > documents.tar.gz
```

Получите утверждённую версию checkout, соберите/опубликуйте новый image и повторите deploy.py
с новым digest. При обновлении Secret скрипт также перезапускает app pod. PVC сохраняются;
пароль существующей роли PostgreSQL нельзя поменять только редактированием Secret.
Не удаляйте namespace, StatefulSet PVC или claims при обновлении. Текущий StorageClass имеет
reclaimPolicy Delete: удаление claims может удалить диски. Backup храните отдельно от кластера.
Ротация пароля и восстановление backup выполняются отдельно; rollback image требует проверки
совместимости схемы. Обычный повтор deployment сохраняет историю и scheduler deadlines.

```sh
kubectl --context yc-big-bang -n chief-ai-assistant rollout restart deployment/assistant
kubectl --context yc-big-bang -n chief-ai-assistant rollout status deployment/assistant --timeout=300s
kubectl --context yc-big-bang -n chief-ai-assistant get pods,pvc
kubectl --context yc-big-bang -n chief-ai-assistant exec deploy/assistant -c backend -- \
  python -m app.health telegram
kubectl --context yc-big-bang -n chief-ai-assistant exec deploy/assistant -c backend -- \
  python -m app.health background
```

После обновления спросите сохранённый факт, задайте вопрос по прежнему PDF, подтвердите
прежнее pending approval и проверьте напоминание. Реальные кнопки нажимает владелец;
benchmark создаёт pending approval и не подтверждает его автоматически.
Клиентская приёмка: [../../../docs/acceptance.md](../../../docs/acceptance.md).
