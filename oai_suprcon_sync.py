import argparse
import logging
import os
import time
import requests
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler

import psycopg2
from psycopg2.extras import execute_values


BASE = "https://oaipmh.arxiv.org/oai"
SET_SPEC = "physics:cond-mat"
SOURCE_KEY = "oai:physics:cond-mat:supr-con"

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "arxiv": "http://arxiv.org/OAI/arXiv/"
}


# -------------------- logging --------------------

def setup_logging(log_file: str | None):
    """
    Если log_file=None или пустой -> лог только в stdout.
    Если указан -> RotatingFileHandler + stdout.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    handlers = []

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    handlers.append(sh)

    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        handlers.append(fh)

    logger.handlers.clear()
    for h in handlers:
        logger.addHandler(h)


# -------------------- http/xml --------------------

def fetch(params):
    r = requests.get(BASE, params=params, timeout=60)
    r.raise_for_status()
    return r.text


def normalize_ws(s: str) -> str:
    return " ".join((s or "").split())


def is_suprcon_category(cat: str) -> bool:
    cat = (cat or "").strip().lower()
    return cat.endswith(".supr-con") or cat == "supr-con"


def classify_id(arxiv_id: str):
    s = (arxiv_id or "").strip()
    if "/" in s:
        cat, rest = s.split("/", 1)
        yymm = rest[:4] if len(rest) >= 4 else "0000"
        lookup_key = f"{cat}{rest}"  # remove slash
        return "old", yymm, lookup_key

    if "." in s:
        # YYYY.MMNNNNN
        try:
            yyyy, rest = s.split(".", 1)
            mm = rest[:2]
            yymm = yyyy[2:4] + mm
            return "new", yymm, s
        except Exception:
            pass

    return "other", "0000", s.replace("/", "")


def get_text(elem, path):
    child = elem.find(path, NS)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def parse_records(xml_text):
    root = ET.fromstring(xml_text)
    rows = []
    max_ds = None

    for rec in root.findall(".//oai:record", NS):
        header = rec.find("oai:header", NS)
        if header is None:
            continue

        datestamp_str = get_text(header, "oai:datestamp")
        ds = None
        if datestamp_str:
            ds = date.fromisoformat(datestamp_str[:10])
            if max_ds is None or ds > max_ds:
                max_ds = ds

        is_deleted = (header.get("status") == "deleted")

        meta = rec.find("oai:metadata/arxiv:arXiv", NS)
        if meta is None:
            continue

        arxiv_id = get_text(meta, "arxiv:id")
        if not arxiv_id:
            continue

        cats_raw = normalize_ws(get_text(meta, "arxiv:categories"))
        cat_tokens = cats_raw.split() if cats_raw else []

        # строго: только если присутствует supr-con
        if not any(is_suprcon_category(c) for c in cat_tokens):
            continue

        title = normalize_ws(get_text(meta, "arxiv:title"))
        id_type, yymm, lookup_key = classify_id(arxiv_id)

        rows.append((
            arxiv_id,
            title,
            cats_raw,
            id_type,
            yymm,
            lookup_key,
            ds,
            is_deleted
        ))

    token_el = root.find(".//oai:resumptionToken", NS)
    token = token_el.text.strip() if token_el is not None and token_el.text else None
    return rows, token, max_ds


# -------------------- sync_state helpers --------------------

def ensure_sync_state(cur):
    cur.execute("""
        insert into sync_state(source, updated_at)
        values (%s, now())
        on conflict (source) do nothing
    """, (SOURCE_KEY,))


def mark_running(cur, note: str | None):
    cur.execute("""
        update sync_state
        set
          last_status = 'RUNNING',
          last_error = null,
          last_run_started_at = now(),
          last_run_finished_at = null,
          updated_at = now(),
          note = %s
        where source = %s
    """, (note, SOURCE_KEY))


def mark_success(cur, *, rows_written: int, checkpoint_ds: date | None):
    cur.execute("""
        update sync_state
        set
          last_status = 'OK',
          last_error = null,
          last_run_finished_at = now(),
          last_success_at = now(),
          last_success_datestamp = %s,
          last_rows = %s,
          total_rows = total_rows + %s,
          updated_at = now()
        where source = %s
    """, (checkpoint_ds, rows_written, rows_written, SOURCE_KEY))


def mark_error(cur, *, err: str):
    cur.execute("""
        update sync_state
        set
          last_status = 'ERROR',
          last_error = left(%s, 8000),
          last_run_finished_at = now(),
          last_rows = 0,
          updated_at = now()
        where source = %s
    """, (err, SOURCE_KEY))


def get_checkpoint(cur):
    cur.execute("select last_success_datestamp from sync_state where source=%s", (SOURCE_KEY,))
    row = cur.fetchone()
    return row[0] if row else None


def update_checkpoint(cur, new_ds: date):
    cur.execute("""
        update sync_state
        set last_success_datestamp=%s, updated_at=now()
        where source=%s
    """, (new_ds, SOURCE_KEY))


# -------------------- main DB logic --------------------

def upsert_rows(cur, rows):
    if not rows:
        return 0

    # ВАЖНО: не трогаем arxiv_paper.status/attempts/last_error
    sql = """
    insert into arxiv_paper(
      arxiv_id, title, categories, id_type, yymm, lookup_key,
      oai_datestamp, is_deleted
    )
    values %s
    on conflict (arxiv_id) do update set
      title         = excluded.title,
      categories    = excluded.categories,
      id_type       = excluded.id_type,
      yymm          = excluded.yymm,
      lookup_key    = excluded.lookup_key,
      oai_datestamp = excluded.oai_datestamp,
      is_deleted    = excluded.is_deleted,
      updated_at    = now(),
      last_seen_at  = now();
    """
    execute_values(cur, sql, rows, page_size=2000)
    return len(rows)


def run_once(conn, overlap_days: int, polite_sleep: float):
    total = 0
    max_seen = None
    token = None

    with conn.cursor() as cur:
        ensure_sync_state(cur)
        # note полезно для дебага
        mark_running(cur, note=f"set={SET_SPEC}, overlap_days={overlap_days}, polite_sleep={polite_sleep}")
        checkpoint = get_checkpoint(cur)
        conn.commit()

    date_from = None
    if checkpoint:
        date_from = checkpoint - timedelta(days=overlap_days)

    logging.info("Sync start. checkpoint=%s from=%s overlap_days=%d", checkpoint, date_from, overlap_days)

    while True:
        if token:
            xml_text = fetch({"verb": "ListRecords", "resumptionToken": token})
        else:
            params = {"verb": "ListRecords", "metadataPrefix": "arXiv", "set": SET_SPEC}
            if date_from:
                params["from"] = date_from.isoformat()
            xml_text = fetch(params)

        rows, token, batch_max_ds = parse_records(xml_text)

        with conn.cursor() as cur:
            written = upsert_rows(cur, rows)
            conn.commit()

        total += written
        if batch_max_ds and (max_seen is None or batch_max_ds > max_seen):
            max_seen = batch_max_ds

        logging.info("Page processed. wrote=%d total=%d token=%s max_ds=%s",
                     written, total, bool(token), max_seen)

        if not token:
            break

        time.sleep(polite_sleep)

    if max_seen:
        with conn.cursor() as cur:
            update_checkpoint(cur, max_seen)
            conn.commit()

    with conn.cursor() as cur:
        mark_success(cur, rows_written=total, checkpoint_ds=max_seen)
        conn.commit()

    logging.info("Sync done. total_written=%d checkpoint_now=%s", total, max_seen)
    return total, max_seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", required=True, help="postgresql://user:pass@host:port/db")
    ap.add_argument("--overlap-days", type=int, default=2)
    ap.add_argument("--polite-sleep", type=float, default=0.5)
    ap.add_argument("--log-file", default=None, help="путь до файла лога. если не задан -> только stdout")
    args = ap.parse_args()

    setup_logging(args.log_file)
    logging.info("START oai_suprcon_sync one-shot")

    conn = psycopg2.connect(args.pg)
    conn.autocommit = False
    try:
        run_once(conn, args.overlap_days, args.polite_sleep)
    except Exception as e:
        logging.exception("FATAL ERROR")
        try:
            with conn.cursor() as cur:
                ensure_sync_state(cur)
                mark_error(cur, err=repr(e))
                conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
