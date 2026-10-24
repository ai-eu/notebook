# AGENTS.md — Notebook / iPhone Audio Transcription Service

## Описание проекта

FastAPI веб-приложение для загрузки аудио/видео, автоматической конвертации в MP3, транскрибации через Groq Whisper, прослушивания транскриптов с подсветкой предложений, TTS и Telegram-архива.

## Стек

- Backend: FastAPI, Uvicorn, SQLAlchemy 2.0 (async, aiosqlite), Pydantic Settings
- Аудио: ffmpeg / ffprobe (конвертация, нарезка, длительность, битрейт)
- Транскрибация: Groq Whisper (`whisper-large-v3`)
- Smart-форматирование: Groq Chat API
- TTS: edge-tts, OpenAI, Grok, Azure Speech, Google TTS
- Шумоподавление: noisereduce, soundfile
- Тесты: pytest

## Структура

```
app/
  main.py           — точка входа, фоновый maintenance loop
  config.py         — настройки (.env через pydantic-settings)
  database.py       — async engine, init_db с миграциями колонок
  models.py         — ORM: User, Session, Recording, StoredFile, DeviceInvite
  auth.py           — аутентификация, сессии, device code, rate-limit
  utils.py          — хелперы (пути, ID записи, safe_delete)
  routers/          — auth, pages, upload, recordings, tts
  services/         — бизнес-логика
    converter.py    — ffmpeg/ffprobe обёртка
    denoiser.py     — шумоподавление (noisereduce)
    transcriber.py  — Groq Whisper, chunking, mock
    formatter.py    — абзацы, предложения, Groq smart split
    groq.py         — валидация ключей Groq
    processing.py   — конвейер обработки (транскрибация + TTS)
    tts.py          — синтез речи, провайдеры
    storage/        — archive.py, cache.py, maintenance.py, telegram.py
  cli.py            — CLI: пользователи, ключи, Telegram-архив
  templates/        — Jinja2 HTML
  static/           — CSS, JS
deploy/             — systemd сервис, nginx.conf
docs/               — TTS_SPEC.md, donoiser.md
tests/              — pytest (~20 файлов)
run.py              — uvicorn entrypoint
requirements.txt
.env.example
README.md
```

## Как запустить

```bash
cp .env.example .env   # отредактировать .env
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
# source venv/bin/activate
pip install -r requirements.txt

# Без перезагрузки (фоновые задачи не убиваются):
venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Открыть http://localhost:8000, войти с Groq API-ключом.

Тестовый режим без Groq:
```bash
MOCK_TRANSCRIPTION=true uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Тесты

```bash
pip install -r requirements-dev.txt
pytest
```

## CLI

```bash
python -m app.cli add-user "Имя" --key gsk_...
python -m app.cli list-users
python -m app.cli rebind-key <user_id> gsk_...
python -m app.cli verify-key <user_id>
python -m app.cli delete-user <user_id> [--purge-files]
python -m app.cli tg-discover
python -m app.cli tg-status
python -m app.cli tg-backfill [--limit N]
python -m app.cli tg-verify [recording_id] [--no-repair]
python -m app.cli tg-restore <recording_id> [--force]
python -m app.cli tg-cleanup [--dry-run]
```

## Важные замечания

- Groq API-ключи хранятся в БД в открытом тексте. `app.db` и `data/` приватные.
- Требуется `ffmpeg` + `ffprobe` в системе.
- В production за nginx с HTTPS, `SESSION_COOKIE_SECURE=true`, `TRUST_PROXY_HEADERS=true`.
- Пути в БД relatives к `data/` — база и данные можно переносить между серверами.

## Направления улучшения (языковое обучение)

См. раздел "Направления развития для изучения языков" ниже.

## Согласия по коду

- Комментарии в коде не добавлять, если не попросят явно.
- Перед правкой файла прочитать его.
- Следовать существующему стилю и соглашениям проекта.
- Не коммитить без явного запроса пользователя.

## Направления развития для изучения языков

### 1. Совмещённая транскрипция и перевод (реализовано)
- Показывать исходный текст и перевод предложений рядом (двойной столбец).
- Перевод на украинский (и другие языки из селектора: uk, pt-PT, es, en) через Groq Chat API.
- Подсветка соответствий между предложениями на двух языках при наведении.
- Колонка перевода скрывается/показывается по кнопке, состояние запоминается в localStorage.
- Перевод кэшируется в `formatted-<lang>.txt` рядом с `formatted.txt` и архивируется в Telegram-канал.
- Настройки: `TRANSLATION_ENABLED`, `TRANSLATION_TARGET_LANGUAGES`, `TRANSLATION_DEFAULT_LANGUAGE`, `TRANSLATION_BATCH_SIZE`.
- API: `GET /api/translate/{recording_id}?lang=<code>`, `DELETE /api/translate/{recording_id}?lang=<code>`.

### 2. Интерактивное обучение по транскрипту
- Карточки «пропущенное слово» (cloze) из транскрипта.
- Режим диктовки: пользователь набирает текст, который только что прослушал, с проверкой ошибок.
- Тренировка аудирования: прослушать фрагмент без текста, затем проверить.

### 3. Анализ лексики и грамматики
- Извлечение ключевых слов и фраз из транскрипта с таймкодами.
- Создание персонального словаря из записей пользователя.
- Статистика по словам: частота, предложения где встречалось, аудио-пример.
- Грамматические пометки к предложениям (через LLM).

### 4. Практика речи (speaking)
- Запись пользователя с comparación к оригиналу (waveform overlay, оценка совпадения).
- Тренировка произношения отдельных предложений с повторением после говорящего.
- Оценка плавности и интонации (через аудио-анализ).

### 5. Работа с аудио
- Медленное воспроизведение (0.5x–2.0x) с сохранением тона.
- Циклическое воспроизведение отдельного сегмента.
- Тишина между предложениями для паузы и повторения.
- Выделение незнакомых слов с однократным прослушиванием.

### 6. Прогресс и геймификация
- Ежедневная цель: прослушано минут, слов изучено.
- Серии (streaks) и достижения.
- Сложность текста (CEFR уровень) через анализ лексики.
- Рекомендации следующих записей по уровню.

### 7. Экспорт и интеграция
- Экспорт в Anki (CSV с аудио, текстом, переводом).
- Экспорт в PDF/EPUB с дублированным текстом.
- Экспорт начертания для печати (worksheet).

### 8. Социальное и коллаборативное
- Публичные подборки записей по темам и уровням.
- Комментарии к записям, обсуждение сложных моментов.
- Совместные подборки для группы изучения.

### 9. Мобильный опыт
- PWA для офлайн-доступа к скачанным транскриптам и аудио.
- Push-уведомления о новых записях или целях.
