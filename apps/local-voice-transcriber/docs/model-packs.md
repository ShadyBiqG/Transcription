# Локальные model packs

Во время работы Transcription Next не загружает модели, веса или аудио из сети.
Обычный Windows-установщик один раз доставляет проверенный набор в
`NEXT_DATA_DIR/models`, сверяет каждый файл по SHA-256 и создаёт manifest
`asr-pack.json`. Он также запускает локальную проверку загрузки ASR, VAD,
диаризации, голосового extractor и FFmpeg. Штатный установщик не доставляет
LLM-веса или llama.cpp и принудительно оставляет `NEXT_CLEANER_ENABLED=false`,
чтобы не отнимать CPU и память у ASR. Неверная контрольная сумма или
несовместимый runtime останавливают доставку пакетов до создания учётной
записи.

Для ONNX Runtime на Windows требуется Microsoft Visual C++ 2019 Runtime или
новее. Установщик сначала проверяет импорт, а при отсутствии runtime получает
`vc_redist.x64.exe` только с официальной ссылки Microsoft, проверяет цифровую
подпись и устанавливает его без перезагрузки. Если UAC запрашивает повышение
прав, его нужно подтвердить.

Для сервера из примера это будут `E:\TranscriptionData\models` и
`E:\TranscriptionData\tools`. Первая загрузка составляет приблизительно 1.1 ГБ;
на диске data root требуется не менее 7 ГБ свободного места для временного
каталога и распаковки.

## Состав стандартного пакета

- ASR: GigaAM v3 RNN-T (`gigaam-v3-rnnt`) из закреплённого revision
  `322c3b29492673eb7d0b434bfa9dfb8653e34d02`: `config.json`,
  `v3_rnnt_encoder.onnx`, `v3_rnnt_decoder.onnx`, `v3_rnnt_joint.onnx`,
  `v3_vocab.txt`. Источник:
  [istupakov/gigaam-v3-onnx](https://huggingface.co/istupakov/gigaam-v3-onnx),
  лицензия MIT. RNN-T выбран как дефолтный пакет из-за лучшей точности на
  проверенном наборе onnx-asr; нормализацию и пунктуацию он не гарантирует и
  не является фильтром слов-паразитов. Установщик сверяет SHA-256 и размер
  всех пяти файлов перед публикацией каталога. Прежний E2E-каталог не удаляется:
  manifest переключается только после полной проверки нового RNN-T-пакета.
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
$env:NEXT_CLEANER_MODEL_PATH = ""
$env:NEXT_CLEANER_LLAMA_SERVER_PATH = ""
$env:NEXT_CLEANER_ENDPOINT = "http://127.0.0.1:8020"
$env:NEXT_CLEANER_ENABLED = "false" # LLM cleaner отключён по умолчанию
```

`TranscriptionNextCleanerService` не является частью штатного запуска. При
обновлении установщик останавливает ранее созданную службу и устанавливает ей
тип запуска `Disabled`; `llama-server.exe` не слушает порт 8020. Основная
`TranscriptionNextCandidateService` продолжает работу с rule-based очисткой.
При недоступном локальном LLM приложение должно сохранить исходную
транскрипцию и показать её вместо изменённого текста. Пакеты Qwen или llama.cpp,
оставшиеся от прежней версии, не входят в `asr-pack.json` и не используются.

Для одного `DataPath` разрешён ровно один процесс приложения. Каталог ZeusDB
расположен внутри `NEXT_DATA_DIR`, хранит общий голосовой индекс и должен быть
доступен только сервисной учётной записи. SQLite хранит права, аудит и outbox,
но не содержит bytes голосовых эмбеддингов.

Для автоматического сопоставления всё равно нужна отдельная калибровка порогов
на согласованном локальном корпусе. До неё приложение оставляет неизвестные
группы для ручного решения в редакторе. После калибровки оператор явно задаёт
пороги и включает `NEXT_VOICE_CALIBRATED=true` и
`NEXT_VOICE_AUTO_ASSIGN_ENABLED=true`.

## Число локальных спикеров

Новые запуски диаризации Sherpa-ONNX оставляют число спикеров автоматическим
(`num_speakers=-1`) и используют порог кластеризации `0.9` вместо `0.5`.
По [документации Sherpa-ONNX](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)
больший порог создаёт меньше групп; `0.9` приведён там как пример для записи с
неизвестным числом спикеров. Минимальная длительность речевого фрагмента снижена
до `0.1` с, чтобы реже отбрасывать короткие реплики: Sherpa удаляет фрагменты
короче [`min_duration_on`](https://k2-fsa.github.io/sherpa/onnx/c-api/html/structSherpaOnnxOfflineSpeakerDiarizationConfig.html).
Это не гарантирует правильного числа участников на каждой записи. Имена,
разделение и объединение групп необходимо сверять вручную. Уже опубликованные
результаты и редакторские ревизии не пересчитываются.
