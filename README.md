# oai_suprcon_sync

Проект массово и инкрементально загружает записи из OAI-PMH arXiv, фильтрует статьи по наличию категории `supr-con` среди категорий записи и сохраняет базовые метаданные в PostgreSQL таблицу `arxiv_paper`.

---

## Что делает проект

`oai_suprcon_sync.py`:

- ходит в OAI-PMH endpoint arXiv;
- читает `ListRecords` и обрабатывает `resumptionToken`;
- берёт записи из набора `physics:cond-mat`;
- фильтрует их так, чтобы остались только статьи, где среди категорий присутствует `supr-con` / `*.supr-con`;
- нормализует `arxiv_id`;
- вычисляет `id_type`, `yymm`, `lookup_key`;
- сохраняет записи в `arxiv_paper`;
- ведёт checkpoint и статус синхронизации в `sync_state`.

---

## Зачем это нужно

Это входная точка всего пайплайна.

Именно этот проект формирует базовый список статей, которые дальше:

- будут сопоставлены с tar-архивами;
- будут проиндексированы по содержимому tar;
- будут скачаны как PDF и переложены в целевое S3;
- могут быть потом отправлены в RAG / vector store / downstream NLP-пайплайн.

---

## Источник данных

- OAI-PMH endpoint arXiv: `https://oaipmh.arxiv.org/oai`
- документация arXiv OAI-PMH: <https://info.arxiv.org/help/oa/index.html>

---

## Требования

- Python 3.10+
- PostgreSQL
- `requests`
- `psycopg2-binary`

Установка:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install requests psycopg2-binary
```

---

## Быстрый старт

```bash
python oai_suprcon_sync.py \
  --pg postgresql://postgres:postgres@localhost:5432/rag \
  --overlap-days 2 \
  --polite-sleep 0.5
```

С логированием в файл:

```bash
python oai_suprcon_sync.py \
  --pg postgresql://postgres:postgres@localhost:5432/rag \
  --overlap-days 2 \
  --polite-sleep 0.5 \
  --log-file /var/log/oai_suprcon_sync.log
```

---

## Аргументы CLI

```text
--pg            Postgres DSN
--overlap-days  сколько дней перекрытия брать от последнего успешного checkpoint
--polite-sleep  пауза между запросами к OAI-PMH
--log-file      путь к лог-файлу, опционально
```

---

## Что сохраняется в БД

### Таблица `arxiv_paper`

Минимальная схема, совместимая с текущей логикой проекта:

```sql
CREATE TABLE IF NOT EXISTS arxiv_paper (
  arxiv_id       text PRIMARY KEY,
  title          text,
  categories     text,
  id_type        text,
  yymm           char(4),
  lookup_key     text,
  oai_datestamp  date,
  is_deleted     boolean NOT NULL DEFAULT false,
  payload        jsonb,

  status         text DEFAULT 'NEW',
  worker_id      text,
  locked_at      timestamptz,
  attempts       integer NOT NULL DEFAULT 0,
  last_error     text,
  updated_at     timestamptz NOT NULL DEFAULT now()
);
```

### Индексы

```sql
CREATE INDEX IF NOT EXISTS idx_arxiv_paper_lookup_key
  ON arxiv_paper(lookup_key);

CREATE INDEX IF NOT EXISTS idx_arxiv_paper_yymm
  ON arxiv_paper(yymm);

CREATE INDEX IF NOT EXISTS idx_arxiv_paper_status
  ON arxiv_paper(status);

CREATE INDEX IF NOT EXISTS idx_arxiv_paper_oai_datestamp
  ON arxiv_paper(oai_datestamp);
```

### Таблица `sync_state`

```sql
CREATE TABLE IF NOT EXISTS sync_state (
  source                  text PRIMARY KEY,
  last_status             text,
  last_error              text,
  last_run_started_at     timestamptz,
  last_run_finished_at    timestamptz,
  last_success_at         timestamptz,
  last_success_datestamp  date,
  last_rows               bigint DEFAULT 0,
  total_rows              bigint DEFAULT 0,
  note                    text,
  updated_at              timestamptz NOT NULL DEFAULT now()
);
```

---

## Как вычисляются поля

### `id_type`

- `old` — старый формат id вроде `cond-mat/9801001`
- `new` — новый формат вроде `2501.01234`
- `other` — fallback, если формат распознать не удалось

### `yymm`

Используется для группировки и сопоставления с tar-архивами.

Примеры:

- `2501.01234` -> `2501`
- `cond-mat/9801001` -> `9801`

### `lookup_key`

Нормализованный ключ для дальнейшего поиска PDF внутри tar.

Примеры:

- `2501.01234` -> `2501.01234`
- `cond-mat/9801001` -> `cond-mat9801001`

---

## Логика инкрементальной синхронизации

1. Проект читает checkpoint из `sync_state.last_success_datestamp`.
2. Отступает назад на `overlap-days`.
3. Делает OAI-PMH `ListRecords` с параметром `from`.
4. Обрабатывает все страницы через `resumptionToken`.
5. После успешной обработки обновляет checkpoint.

Зачем нужен overlap:

- чтобы не потерять записи на границах дат;
- чтобы переживать частичные сбои и повторные запуски.

---

## Почему используется `ON CONFLICT DO NOTHING`

Текущая реализация не перетирает существующие строки в `arxiv_paper`, что делает повторные запуски безопасными и не ломает downstream-статусы, связанные с обработкой PDF.

Если понадобится полноценное обновление metadata, логику можно расширить до selective upsert.

---

## Проверка результата

### Сколько статей загружено

```sql
SELECT count(*)
FROM arxiv_paper;
```

### Сколько статей по год-месяцу

```sql
SELECT yymm, count(*)
FROM arxiv_paper
GROUP BY yymm
ORDER BY yymm DESC;
```

### Пример выборки superconductivity-статей

```sql
SELECT arxiv_id, title, categories, yymm, lookup_key
FROM arxiv_paper
ORDER BY oai_datestamp DESC
LIMIT 20;
```

### Состояние последней синхронизации

```sql
SELECT *
FROM sync_state
WHERE source = 'oai:physics:cond-mat:supr-con';
```

---

## Типовые проблемы

### OAI-PMH отвечает медленно

Увеличьте `polite-sleep` и проверьте сеть. Проект уже использует таймауты для HTTP-запроса.

### Дубликаты

Повторные запуски безопасны из-за `ON CONFLICT DO NOTHING`.

### Записи не попадают в выборку

Проверьте, что среди категорий записи действительно есть `supr-con` или категория, оканчивающаяся на `.supr-con`.

---

## Идеи для развития

- добавить selective upsert вместо `DO NOTHING`;
- отдельно сохранять авторов, abstract и DOI;
- вынести DDL в миграции;
- добавить Dockerfile и cron/systemd примеры;
- экспортировать метрики по длительности и числу обработанных страниц.
