"""
main.py
Coordinates Slack local cache discovery, message extraction via SlackCache,
and synchronization to Google Sheets via Google Apps Script.
"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from slack_cache import SlackCache

DIR = Path(__file__).resolve().parent
CONFIG_FILE = DIR / "config.json"
LOG_FILE = DIR / "activity.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout) if sys.stdout else logging.NullHandler(),
    ],
)
logger = logging.getLogger("slack_sync")

# Load configuration from config.json
if not CONFIG_FILE.exists():
    logger.error("config.json not found in %s", DIR)
    raise FileNotFoundError(f"config.json must be in the same folder as main.py: {CONFIG_FILE}")

try:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
except Exception as e:
    logger.exception("Failed to parse config.json: %s", e)
    raise

WEB_APP_URL = config.get("WEB_APP_URL", "")
SECRET = config.get("SECRET", "")
INCLUDE_DMS = config.get("INCLUDE_DMS", True)
BATCH_SIZE = config.get("BATCH_SIZE", 2000)


def get_channel_label(channel_id: str, names: dict) -> str:
    if not channel_id:
        return ""
    if channel_id.startswith("D"):
        partner_id = names["dm_with"].get(channel_id, "")
        partner_name = names["people"].get(partner_id)
        return f"DM: {partner_name}" if partner_name else "DM (unknown)"
    return names["channels"].get(channel_id, "")


def build_sheet_rows(df: pd.DataFrame, names: dict) -> list:
    """Formats DataFrame records to match the Google Sheet column order."""
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for r in df.itertuples(index=False):
        cid = str(r.channel or "")
        uid = str(r.user or "")
        rows.append([
            get_channel_label(cid, names),
            cid,
            names["people"].get(uid, ""),
            uid,
            r.text[:49000],
            str(r.thread_ts or ""),
            r.ts,
            r.time_utc.strftime("%Y-%m-%d %H:%M:%S") if pd.notnull(r.time_utc) else "",
            now,
            names["workspace"].get("name", ""),
            names["workspace"].get("domain", ""),
            str(getattr(r, "reactions", "") or ""),
            str(getattr(r, "attachments", "") or ""),
        ])
    return rows


def push_to_sheet(rows: list, user_name: str, messages_found: int, status_note: str) -> int:
    """Sends rows and run telemetry to Google Apps Script."""
    payload = {
        "secret": SECRET,
        "user": user_name,
        "messages_found": messages_found,
        "status": status_note,
        "rows": rows[:BATCH_SIZE] if rows else [],
    }

    resp = requests.post(WEB_APP_URL, json=payload, timeout=300)
    resp.raise_for_status()

    result = resp.json()
    if result.get("status") != "success":
        raise RuntimeError(f"Sheet rejected upload: {result.get('message')}")

    added = result.get("rows_added", 0)

    # If rows exceed the first batch, send remaining rows in subsequent batches
    for i in range(BATCH_SIZE, len(rows), BATCH_SIZE):
        batch_payload = {
            "secret": SECRET,
            "user": user_name,
            "messages_found": messages_found,
            "status": "batch_continue",
            "rows": rows[i:i + BATCH_SIZE],
        }
        batch_resp = requests.post(WEB_APP_URL, json=batch_payload, timeout=300)
        batch_resp.raise_for_status()
        batch_res = batch_resp.json()
        if batch_res.get("status") == "success":
            added += batch_res.get("rows_added", 0)

    return added


def main():
    if not WEB_APP_URL or "PASTE_" in WEB_APP_URL or not SECRET or "PASTE_" in SECRET:
        raise RuntimeError("Configure valid WEB_APP_URL and SECRET in config.json before running.")

    logger.info("Discovering Slack cache locations...")
    extractor = SlackCache(include_dms=INCLUDE_DMS)
    candidates = extractor.get_candidates()

    if not candidates:
        raise FileNotFoundError("No Slack IndexedDB cache found on this machine.")

    logger.info("Found %d candidate cache folder(s)", len(candidates))

    selected_result = None

    for idx, (cache_path, mtime) in enumerate(candidates, 1):
        dt = datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else "unknown"
        logger.info("Testing candidate [%d/%d]: %s (last write: %s)", idx, len(candidates), cache_path.name, dt)

        try:
            result = extractor.extract(cache_path)
            logger.info("Scanned %d records (%d stores skipped)", result.records_scanned, result.skipped_stores)
        except Exception as e:
            logger.warning("Extraction failed for %s: %s", cache_path.name, e)
            continue

        if len(result.df) > 0:
            selected_result = result
            logger.info("Extracted %d message(s) for user '%s'", len(result.df), result.current_user)
            break
        else:
            if selected_result is None:
                selected_result = result
            logger.info("Cache %s contained 0 messages. Testing next candidate...", cache_path.name)

    if selected_result is None:
        raise RuntimeError("All candidate caches were scanned, but extraction failed.")

    total_msgs = len(selected_result.df)
    user_name = selected_result.current_user

    if total_msgs == 0:
        logger.info("0 messages found in cache (quiet period). Notifying Google Sheets log tab...")
        try:
            push_to_sheet([], user_name=user_name, messages_found=0, status_note="Quiet period (0 messages)")
        except Exception as e:
            logger.warning("Could not record quiet period in Google Sheet: %s", e)
        logger.info("Sync finished successfully. 0 new messages.")
        return

    rows = build_sheet_rows(selected_result.df, selected_result.names)
    logger.info("Uploading %d message(s) to Google Sheets for user '%s'...", len(rows), user_name)

    added = push_to_sheet(rows, user_name=user_name, messages_found=total_msgs, status_note="Messages synced")
    logger.info("Sync complete. Found %d messages, added %d new to Google Sheets.", total_msgs, added)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("Slack sync execution failed: %s", e)
        sys.exit(1)
