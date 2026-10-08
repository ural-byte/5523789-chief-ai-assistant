# URALBYTE-8: обновление bigbang, 2026-10-04

Executable/source: `d39e998845b6e105f88e9dcd92827143ef4023b6`.
Immutable image: `cr.yandex/crp5er6iam3h65oe4kdd/chief-ai-assistant@sha256:454f4733fb346715ff9c8d7d95a9f3b88d60b879d760400439a7f051ca563c3d`.
AMD64 child manifest: `sha256:15758473a8393c58079f9d2a259d7594503be627411be742e5bf9d42fa7283b9`.
OCI revision совпадает с source commit. Последующие изменения документов не меняют executable.
[PR2](https://github.com/ural-byte/5523789-chief-ai-assistant/pull/2) связан с
[URALBYTE-8](https://tracker.yandex.ru/URALBYTE-8), основан на ещё не слитом PR1.
[CI push](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37217772013)
и [CI PR](https://github.com/ural-byte/5523789-chief-ai-assistant/actions/runs/37217774641)
для exact source HEAD — success.

## Проверенные факты

- Context `yc-big-bang`, namespace `chief-ai-assistant`; app 3/3 Ready, DB 1/1 Ready,
  backend health и оба worker heartbeat PASS. Migration `0004`.
- Те же PVC: documents 2Gi / `pvc-20c68842-e5ec-476f-b161-047159f978de`,
  postgres-db-0 10Gi / `pvc-148f28c3-fe05-495c-b232-f2fd0dd5b430`, Bound/RWO.
  Namespace и volumes не удалялись; БД не переинициализировалась.
- После Recreate совпали контрольные суммы двух сохранённых записей памяти,
  двух документов и их файлов, пяти поручений и immutable payload одной встречи.
  Эти количества сняты до добавления новых контрольных fixtures.
- Независимые SOURCE Checker PASS: 122 теста PostgreSQL/pgvector + 14 отдельных
  adversarial cases, Ruff/diff-check, migration upgrade/check/roundtrip PASS.
  SOURCE Reviewer APPROVE после одного цикла исправления гонки pending memory producer.
- [Latency до](../measurements/2026-10-04-ux-latency-before.json) и
  [после](../measurements/2026-10-04-ux-latency-after.json) независимо сверены с БД:
  три обзора памяти 1705/935/787 мс ingress → Telegram ACK, 0 AI; явный semantic lookup
  5527 мс, настоящий Yandex, источник проверен. Первоначальный неоднозначный контроль
  выбрал PDF clarification; его расход сохранён.
- [PDF feedback](../measurements/2026-10-04-ux-feedback.json): typing ACK через 613 мс,
  одно фактическое product progress, true embedding/index ready/final ACK.
  Controlled local upload ждёт progress, поэтому его e2e не оценивает скорость обработки PDF.
- Интервалы обработки и доставки могут перекрываться при раннем каноническом ответе.
  Все final rows done, recorded ACK совпадает с их максимальным фактическим ACK;
  `delivery_ms=null` при ACK до завершения обработки — неприменимый интервал.

Сохранён прежний явный Telegram hostAliases overlay `149.154.167.220`, с обычной
проверкой CA/hostname TLS. Другие приложения, cluster DNS/VPC/маршруты не менялись.
Внешняя доступность Telegram остаётся известным ограничением; из трёх запросов SLA
не выводится. Controlled requests обходят Telegram receiving.

## Продолжение клиентской приёмки

Владелец приглашён отправить общий вопрос памяти, затем `/reset`, прочитать точную
необратимую карточку и нажать «Подтвердить». Настоящий incoming query и owner callback
пока ожидаются; synthetic confirmation не выполнялся. Состояние чистого пользователя
не заявлено. Общий клиентский вердикт **PENDING**, инженерная готовность исходного MVP
не отменяется. Итоговые live gates доработки дополняются после фактической проверки.
