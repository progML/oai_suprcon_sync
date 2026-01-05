# arxiv supr-con sync
Скрипт для **массовой (и затем инкрементальной) выгрузки** статей arXiv из OAI-PMH по категории:
- `cond-mat.supr-con` (статья попадает в базу только если эта категория присутствует в списке категорий)

Источник OAI-PMH:  
https://info.arxiv.org/help/oa/index.html

---

## Возможности

### Разовый запуск

```bash
python .\oai_suprcon_sync.py `
  --pg "postgresql://postgres:postgres@localhost:5432/Rag" `
  --overlap-days 2 `
  --polite-sleep 0.5 `
  --log-file ".\logs\oai_sync.log"
```
---

## Требования

- Python 3.10+
- PostgreSQL 14+ (или 15/16/17)

---

## Описание используемых сущностей бд (создаем вручную)

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
      'NOT_FOUND',
      'ERROR'
    );
  END IF;
END $$;
```

### Таблица `arxiv_paper`

Хранит статьи arXiv, отфильтрованные по категории `cond-mat.supr-con`.

```sql
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

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS arxiv_paper_status_yymm_idx
  ON arxiv_paper(status, yymm);

CREATE INDEX IF NOT EXISTS arxiv_paper_oai_datestamp_idx
  ON arxiv_paper(oai_datestamp);
```


### Таблица `sync_state`

Хранит состояние инкрементальной синхронизации OAI-PMH.

```sql
CREATE TABLE IF NOT EXISTS sync_state (
  source                TEXT PRIMARY KEY,
  last_success_datestamp DATE,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO sync_state(source, last_success_datestamp)
VALUES ('oai:physics:cond-mat:supr-con', NULL)
ON CONFLICT (source) DO NOTHING;

```