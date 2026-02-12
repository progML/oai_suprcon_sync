# arxiv supr-con sync
Скрипт для **массовой (и затем инкрементальной) выгрузки** статей arXiv из OAI-PMH по категории:
- `cond-mat.supr-con` (статья попадает в базу только если эта категория присутствует в списке категорий)

Источник OAI-PMH:  
https://info.arxiv.org/help/oa/index.html

---

## Возможности

### Разовый запуск

```bash
./.venv/bin/python oai_suprcon_sync.py \
  --pg postgresql://rag_user:postgres@localhost:5432/rag \
  --overlap-days 2 \
  --polite-sleep 0.5
```

--pg Строка подключения к PostgreSQL
--overlap-days 2 Страховка от пропущенных данных при инкрементальной синхронизации (минус 2 дня)
--polite-sleep 0.5 Пауза между HTTP-запросами к arXiv (в секундах)
---

## Требования

- Python 3.10+
- PostgreSQL 14+ (или 15/16/17)

---

##  Поднятие postgres и описание используемых сущностей бд (создаем вручную)

### Установка PostgresSQL

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib

sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### Создания пользователя и пароля в субд

```bash
sudo -u postgres psql
```

Вставить конфиг 

```
-- пользователь
CREATE USER rag_user WITH PASSWORD 'postgres';

-- база
CREATE DATABASE rag
  OWNER rag_user
  ENCODING 'UTF8';

-- права
GRANT ALL PRIVILEGES ON DATABASE rag TO rag_user;
```

```bash
\q
```

---

### Используемые сущности


### Тип `paper_status`

Используется для хранения состояния обработки статьи.

```sql
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_type WHERE typname = 'paper_status'
  ) THEN
    CREATE TYPE paper_status AS ENUM (
      'NEW',
      'DOWNLOADING',
      'DONE',
      'COMPLETED',
      'NOT_FOUND',
      'ERROR'
    );
  END IF;
END $$;
```

### Таблица `arxiv_paper`

Хранит статьи arXiv, отфильтрованные по категории `cond-mat.supr-con`.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS arxiv_paper (
  arxiv_id      TEXT PRIMARY KEY,
  title         TEXT,
  categories    TEXT,

  id_type       TEXT NOT NULL,
  yymm          CHAR(4) NOT NULL,
  lookup_key    TEXT NOT NULL,

  oai_datestamp DATE,
  is_deleted    BOOLEAN NOT NULL DEFAULT FALSE,

  status        paper_status NOT NULL DEFAULT 'NEW',
  attempts      INT NOT NULL DEFAULT 0,
  last_error    TEXT,
  
  payload       JSONB,
  embedding     vector(768),

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- какой воркер обрабатывает
    locked_by       text,

    -- момент захвата задачи
    locked_at       timestamptz,

    -- heartbeat (воркер жив)
    heartbeat_at    timestamptz
);

CREATE INDEX IF NOT EXISTS arxiv_paper_status_yymm_idx
  ON arxiv_paper(status, yymm);

CREATE INDEX IF NOT EXISTS arxiv_paper_oai_datestamp_idx
  ON arxiv_paper(oai_datestamp);
  
CREATE INDEX IF NOT EXISTS arxiv_paper_payload_idx
  ON arxiv_paper
  USING gin(payload);
  
CREATE INDEX IF NOT EXISTS arxiv_paper_embedding_idx
  ON arxiv_paper
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);
```




### Таблица `sync_state`

Хранит состояние инкрементальной синхронизации OAI-PMH.

```sql
-- Универсальный статус
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sync_status') THEN
    CREATE TYPE sync_status AS ENUM ('RUNNING', 'OK', 'ERROR');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS sync_state (
  source                TEXT PRIMARY KEY,               -- уникальный ключ джоба, например: 'oai:physics:cond-mat:supr-con' или 'manifest:arxiv_pdf_manifest'

  last_status           sync_status,                    -- RUNNING/OK/ERROR
  last_error            TEXT,                           -- последняя ошибка (если была)

  last_run_started_at   TIMESTAMPTZ,                    -- когда стартанул последний запуск
  last_run_finished_at  TIMESTAMPTZ,                    -- когда закончился последний запуск
  last_success_at       TIMESTAMPTZ,                    -- когда был последний успешный запуск

  -- OAI-специфично (точка инкрементального обхода):
  last_success_datestamp DATE,                          -- последний успешно обработанный OAI datestamp (YYYY-MM-DD)

  -- Метрики/счётчики (универсально):
  last_rows             BIGINT NOT NULL DEFAULT 0,      -- сколько строк записали в последнем запуске
  total_rows            BIGINT NOT NULL DEFAULT 0,      -- накопительный счётчик

  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  note                  TEXT                            -- произвольная заметка (например xml path / режим upsert)
);

CREATE INDEX IF NOT EXISTS sync_state_status_idx
  ON sync_state(last_status);

CREATE INDEX IF NOT EXISTS sync_state_updated_idx
  ON sync_state(updated_at);
```

---

## Разворачивание на сервере

### Установка зависимостей и подтягивание проекта

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
cd /opt
sudo git clone https://github.com/progML/oai_suprcon_sync.git
sudo chown -R $USER:$USER /opt/oai_suprcon_sync
cd /opt/oai_suprcon_sync
```

### Создание виртуального окружения

```bash
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install requests psycopg2-binary
source .venv/bin/activate
pip install -U pip
pip install requests psycopg2-binary
deactivate
```

---
### Создание пользователя (для запуска юнита)

```bash
sudo useradd -r -s /usr/sbin/nologin arxiv
sudo chown -R arxiv:arxiv /opt/oai_suprcon_sync
```

---
### Создание systemd service (юнит для запуска)

```bash
sudo nano /etc/systemd/system/oai_suprcon_sync.service
```

Вставить следующие параметры

```
[Service]
Type=oneshot
User=arxiv
Group=arxiv
WorkingDirectory=/opt/oai_suprcon_sync
ExecStart=/opt/oai_suprcon_sync/.venv/bin/python \
  /opt/oai_suprcon_sync/oai_suprcon_sync.py \
  --pg postgresql://rag_user:postgres@localhost:5432/rag \
  --overlap-days 2 \
  --polite-sleep 0.5
NoNewPrivileges=true
```

### Создание timer


```bash
sudo nano /etc/systemd/system/oai_suprcon_sync.timer
```

Вставить следующие параметры

```
[Unit]
Description=Run arXiv supr-con OAI sync every 60 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=60min
Persistent=true

[Install]
WantedBy=timers.target
```

---

### Запуск

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oai_suprcon_sync.timer
```

Проверка работы таймера

```bash
systemctl list-timers | grep oai_suprcon_sync
```

Просмотр журнала/лог

```bash
journalctl -u oai_suprcon_sync@$(whoami).service -n 100 --no-pager
journalctl -xeu oai_suprcon_sync.service
```
