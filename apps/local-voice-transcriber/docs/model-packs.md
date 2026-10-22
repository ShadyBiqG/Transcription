# Локальные model packs

Во время работы Transcription Next не загружает модели, веса или аудио из сети.
Обычный Windows-установщик один раз доставляет проверенный набор в
`NEXT_DATA_DIR/models`, сверяет каждый файл по SHA-256 и создаёт manifest
`asr-pack.json`. Он также запускает локальную проверку загрузки ASR, VAD,
диаризации, голосового extractor и FFmpeg. Неверная контрольная сумма или
несовместимый runtime останавливают установку до создания учётной записи.

Для ONNX Runtime на Windows требуется Microsoft Visual C++ 2019 Runtime или
новее. Установщик сначала проверяет импорт, а при отсутствии runtime получает
`vc_redist.x64.exe` только с официальной ссылки Microsoft, проверяет цифровую
подпись и устанавливает его без перезагрузки. Если UAC запрашивает повышение
прав, его нужно подтвердить.

Для сервера из примера это будут `E:\TranscriptionData\models` и
`E:\TranscriptionData\tools`. Первая загрузка составляет приблизительно 1 ГБ;
на диске data root требуется не менее 5 ГБ свободного места для временного
каталога и распаковки.

## Состав стандартного пакета

- ASR: GigaAM v3 CTC (`gigaam-v3-ctc`) из закреплённого revision
  `322c3b29492673eb7d0b434bfa9dfb8653e34d02`: `config.json`, `v3_ctc.onnx`,
  `v3_vocab.txt`. Источник: [istupakov/gigaam-v3-onnx](https://huggingface.co/istupakov/gigaam-v3-onnx), лицензия MIT.
- VAD: Silero (`silero_vad.onnx`) из закреплённого revision
  `b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6`. Источник:
  [istupakov/silero-vad-onnx](https://huggingface.co/istupakov/silero-vad-onnx),
  лицензия MIT.
- Диаризация: `sherpa-onnx-pyannote-segmentation-3-0` и голосовой extractor
  `wespeaker_en_voxceleb_resnet34.onnx` (размерность 256) из официальных
  [релизов Sherpa-ONNX](https://github.com/k2-fsa/sherpa-onnx/releases).
  Код Sherpa-ONNX распространяется по Apache-2.0; веса WeSpeaker обучены на
  VoxCeleb и требуют атрибуции CC BY 4.0.
- Преобразование медиа: FFmpeg 8.1 shared GPL build из
  [BtbN FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases). URL
  поставки дополнительно защищён известной SHA-256 архива: при изменении
  «latest»-артефакта установка остановится, пока пакет не будет обновлён в
  коде приложения.

Версии `onnx-asr[cpu]==0.12.0`, `sherpa-onnx==1.13.7` и строго совпадающего
`sherpa-onnx-core==1.13.7` закреплены в `pyproject.toml`. Отдельный core
устраняет Windows-конфликт встроенной ONNX-библиотеки Sherpa с ONNX Runtime,
который иначе может аварийно завершить процесс при создании диаризатора.
Размерность manifest сверяется с фактической размерностью extractor при старте
приложения.

## Manifest и ручная доставка

Manifest находится внутри `NEXT_MODELS_DIR` и содержит только относительные
пути. Все каталоги и файлы должны существовать внутри model root; абсолютные
пути, `..`, символические ссылки за model root и неверные SHA-256 отклоняются.
Стандартный manifest создаёт установщик. Для другого пакета оператор может
создать manifest по schema version 1 с разделами `asr`, необязательным `vad` и
необязательным `diarization`; см. типы `LocalModelPack` и
`LocalDiarizationPack` в исходном коде.

В `candidate.env.ps1` установщик записывает только локальные пути:

```powershell
$env:NEXT_MODELS_DIR = "E:\TranscriptionData\models"
$env:NEXT_ASR_MANIFEST = "E:\TranscriptionData\models\asr-pack.json"
$env:NEXT_FFMPEG_PATH = "E:\TranscriptionData\tools\ffmpeg-n8.1-20261021\bin\ffmpeg.exe"
```

Для одного `DataPath` разрешён ровно один процесс приложения. Каталог ZeusDB
расположен внутри `NEXT_DATA_DIR`, хранит общий голосовой индекс и должен быть
доступен только сервисной учётной записи. SQLite хранит права, аудит и outbox,
но не содержит bytes голосовых эмбеддингов.

Для автоматического сопоставления всё равно нужна отдельная калибровка порогов
на согласованном локальном корпусе. До неё приложение оставляет неизвестные
группы для ручного решения в редакторе. После калибровки оператор явно задаёт
пороги и включает `NEXT_VOICE_CALIBRATED=true` и
`NEXT_VOICE_AUTO_ASSIGN_ENABLED=true`.
