# Документация проекта

- [Настройка `.env`](configuration.md) — все параметры, значения по умолчанию, примеры и диагностика.
- [Развертывание на Windows Server](windows-server-deployment.md) — установка noScribe, Windows-служба, брандмауэр, подключение клиентов и обновление через Git.
- [Архитектура](architecture.md) — компоненты первой части, хранение данных и границы четырёх этапов.
- [Общая продуктовая спецификация](../specs/001-meeting-transcription-assistant/spec.md).
- [Спецификация первой части](../specs/002-local-transcription-service/spec.md), её [план](../specs/002-local-transcription-service/plan.md), [модель данных](../specs/002-local-transcription-service/data-model.md) и [OpenAPI](../specs/002-local-transcription-service/contracts/openapi.yaml).
- [Этап 2: определение участников по кадрам](../specs/003-speaker-frame-attribution/spec.md).
- [Этап 3: перепроверка сомнительных фрагментов](../specs/004-uncertain-segment-recovery/spec.md).
- [Этап 4: резюме встречи и проекты задач](../specs/005-meeting-outcome-analysis/spec.md).

После запуска сервис также публикует интерактивную документацию API:

```text
http://<IP-СЕРВЕРА>:8000/docs
```
