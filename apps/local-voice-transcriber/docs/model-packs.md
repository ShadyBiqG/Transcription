# Локальные model packs

Во время работы Transcription Next не загружает модели, веса или аудио из сети.
Обычный Windows-установщик один раз доставляет проверенный набор в
`NEXT_DATA_DIR/models`, сверяет каждый файл по SHA-256 и создаёт manifest
`asr-pack.json`. Он также запускает локальную проверку загрузки ASR, VAD,
диаризации, голосового extractor и FFmpeg. Для чистого черновика он отдельно
доставляет закреплённые GGUF-веса Qwen и `llama-server.exe`. Затем установщик
службы запускает проверенный локальный LLM только на loopback и включает его
для приложения лишь после health-проверки. Неверная контрольная сумма или
несовместимый runtime останавливают доставку пакетов до создания учётной
записи.

Для ONNX Runtime на Windows требуется Microsoft Visual C++ 2019 Runtime или
новее. Установщик сначала проверяет импорт, а при отсутствии runtime получает
`vc_redist.x64.exe` только с официальной ссылки Microsoft, проверяет цифровую
подпись и устанавливает его без перезагрузки. Если UAC запрашивает повышение
прав, его нужно подтвердить.

Для сервера из примера это будут `E:\TranscriptionData\models` и
`E:\TranscriptionData\tools`. Первая загрузка составляет приблизительно 2.2 ГБ;
на диске data root требуется не менее 7 ГБ свободного места для временного
каталога и распаковки.

## Состав стандартного пакета

- ASR: GigaAM v3 E2E CTC (`gigaam-v3-e2e-ctc`) из закреплённого revision
  `322c3b29492673eb7d0b434bfa9dfb8653e34d02`: `config.json`,
  `v3_e2e_ctc.onnx`, `v3_e2e_ctc_vocab.txt`. Источник:
  [istupakov/gigaam-v3-onnx](https://huggingface.co/istupakov/gigaam-v3-onnx),
  лицензия MIT. E2E-вариант возвращает текст с нормализацией и пунктуацией;
  он не является фильтром слов-паразитов и не гарантирует их удаления.
  При обновлении прежний каталог `gigaam-v3-ctc-322c3b29` не удаляется:
  установщик сначала целиком проверяет отдельный E2E-каталог и только затем
  заменяет manifest.
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
- Локальный очиститель: Qwen2.5-1.5B-Instruct-GGUF в квантизации `Q4_K_M`
  из официального [Qwen/Qwen2.5-1.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF),
  revision `91cad51170dc346986eccefdc2dd33a9da36ead9`. Установщик принимает
  только `qwen2.5-1.5b-instruct-q4_k_m.gguf` размером 1 117 320 736 байт и
  SHA-256 `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`.
  Лицензия модели — Apache-2.0.
- Локальный runtime очистителя: CPU-сборка без AVX
  `llama-b5092-bin-win-noavx-x64.zip` из официального release
  [ggml-org/llama.cpp b5092](https://github.com/ggml-org/llama.cpp/releases/tag/b5092).
  Архив принимается только с SHA-256
  `4a4c6aef72ff13122d56a9ab59acf697603ce9084087427400ae7d1b7ebaef71`;
  до публикации распаковывается во временный каталог, проверяется безопасная
  структура путей и запуск `llama-server.exe --version`. Код llama.cpp
  распространяется по MIT.

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
$env:NEXT_CLEANER_MODEL_PATH = "E:\TranscriptionData\models\cleaner\qwen2.5-1.5b-instruct-q4-k-m-91cad511\qwen2.5-1.5b-instruct-q4_k_m.gguf"
$env:NEXT_CLEANER_LLAMA_SERVER_PATH = "E:\TranscriptionData\tools\llama.cpp-b5092-win-noavx-x64\llama-server.exe"
$env:NEXT_CLEANER_ENDPOINT = "http://127.0.0.1:8020"
$env:NEXT_CLEANER_ENABLED = "true" # только после health-проверки cleaner-службы
```

`TranscriptionNextCleanerService` запускает `llama-server.exe` от
`LocalService` с `--host 127.0.0.1 --port 8020 --ctx-size 4096 --parallel 1`.
Установщик сначала записывает `NEXT_CLEANER_ENABLED=false`, ждёт
`http://127.0.0.1:8020/health` и только затем меняет значение на `true`.
Если загрузка модели, служба или health-проверка не удались, значение остаётся
`false`, а основная `TranscriptionNextCandidateService` продолжает работу с
rule-based очисткой. При недоступном локальном LLM приложение должно сохранить
исходную транскрипцию и показать её вместо изменённого текста. GGUF-веса и
llama.cpp не входят в `asr-pack.json`, потому что это отдельный runtime
очистки, а не ONNX ASR-пакет.

Для одного `DataPath` разрешён ровно один процесс приложения. Каталог ZeusDB
расположен внутри `NEXT_DATA_DIR`, хранит общий голосовой индекс и должен быть
доступен только сервисной учётной записи. SQLite хранит права, аудит и outbox,
но не содержит bytes голосовых эмбеддингов.

Для автоматического сопоставления всё равно нужна отдельная калибровка порогов
на согласованном локальном корпусе. До неё приложение оставляет неизвестные
группы для ручного решения в редакторе. После калибровки оператор явно задаёт
пороги и включает `NEXT_VOICE_CALIBRATED=true` и
`NEXT_VOICE_AUTO_ASSIGN_ENABLED=true`.
