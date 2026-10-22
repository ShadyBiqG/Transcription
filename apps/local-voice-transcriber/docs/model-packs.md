# Локальные model packs

Transcription Next не загружает модели, веса или аудио из сети. Оператор отдельно
проверяет лицензию и происхождение файлов, доставляет их в `NEXT_MODELS_DIR` и
создаёт manifest. Каталог по умолчанию — `NEXT_DATA_DIR/models`; в Git не
добавляются ни веса, ни manifest с производственными путями.

## ASR/VAD manifest

Укажите относительный к model root путь переменной `NEXT_ASR_MANIFEST`. Пример
`asr-pack.json`:

```json
{
  "schema_version": 1,
  "language": "ru",
  "asr": {
    "pack_id": "gigaam-v3-local-2026-10",
    "model_name": "gigaam-v3-ctc",
    "directory": "gigaam-v3",
    "files": {
      "model.onnx": "полный_64_символьный_sha256_в_нижнем_регистре"
    }
  },
  "vad": {
    "pack_id": "silero-vad-local-2026-10",
    "model_name": "silero",
    "directory": "silero-vad",
    "files": {
      "model.onnx": "полный_64_символьный_sha256_в_нижнем_регистре"
    }
  }
}
```

Каждый каталог и файл из manifest должен существовать внутри `NEXT_MODELS_DIR`.
Абсолютные пути, выход через `..` и символические ссылки за model root
отклоняются. Все перечисленные файлы сверяются по SHA-256 перед включением
worker-а.

Для получения значения SHA-256 на Windows:

```powershell
(Get-FileHash C:\ProgramData\TranscriptionNext\models\gigaam-v3\model.onnx -Algorithm SHA256).Hash.ToLower()
```

## Включение worker-а

В файле `candidate.env.ps1` задайте только локальные пути:

```powershell
$env:NEXT_MODELS_DIR = "C:\ProgramData\TranscriptionNext\models"
$env:NEXT_ASR_MANIFEST = "asr-pack.json"
$env:NEXT_FFMPEG_PATH = "C:\Tools\ffmpeg\bin\ffmpeg.exe"
```

После рестарта приложение проверит manifest и FFmpeg. Пока хотя бы один из них
не готов, создание run вернёт `asr_pack_unavailable`; это намеренная безопасная
остановка, а не переход к облачному распознаванию.

## Голосовая диаризация и общие эмбеддинги

Локальный адаптер Sherpa ONNX принимает только предварительно нормализованный
моно PCM WAV из private jobs root и проверяет SHA-256 файлов segmentation и
embedding model до запуска. Для него нужны два файла ONNX в отдельном
подкаталоге `NEXT_MODELS_DIR`; они не загружаются приложением из сети.

ZeusDB хранит только векторы общего голосового справочника. Его каталог
`NEXT_ZEUSDB_DIR` всегда находится внутри `NEXT_DATA_DIR` и должен быть доступен
только сервисной учётной записи. SQLite хранит права, аудит и outbox, но не
содержит embedding bytes.

Включение конкретного voice pack и порогов auto-assignment требует отдельной
калибровки на согласованном локальном корпусе. До такой проверки приложение не
создаёт автоматических образцов и не назначает говорящего по одному совпадению.
