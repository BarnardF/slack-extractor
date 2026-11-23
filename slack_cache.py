"""
slack_cache.py
Object-oriented engine for locating, staging, and extracting Slack Desktop
message history from local IndexedDB storage.
"""
import getpass
import io
import logging
import os
import pathlib
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import emoji
from ccl_chromium_reader import ccl_chromium_indexeddb as idb
from ccl_chromium_reader.storage_formats import ccl_leveldb

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^[CGD][A-Z0-9]{8,}$")
ANY_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{8,}$")
TS_RE = re.compile(r"\d{10}\.\d{6}")
CLIENT_KEY_RE = re.compile(r"^persist:slack-client-([A-Z0-9]+)-([A-Z0-9]+)$")

def parse_emoji(name: str) -> str:
    """Converts Slack reaction shortcode into Unicode emoji, preserving custom names as :custom:."""
    clean = name.strip(":")
    # emoji.emojize resolves standard names, aliases (e.g., +1, thumbsup, tada)
    icon = emoji.emojize(f":{clean}:", language="alias")
    return icon


def format_reactions(rxn_list, people: dict) -> str:
    """Formats raw Slack reaction items into human-readable strings with resolved names."""
    if not rxn_list or not isinstance(rxn_list, list):
        return ""
    parts = []
    for item in rxn_list:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("name") or item.get("baseName") or "").strip()
        if not raw:
            continue
        icon = parse_emoji(raw)
        count = item.get("count", len(item.get("users", [])))
        users = [people.get(u, u) for u in item.get("users", []) if u]
        if users:
            parts.append(f"{icon} {count} ({', '.join(users)})")
        else:
            parts.append(f"{icon} {count}")
    return " | ".join(parts)


def format_attachments(files, redux_files: dict) -> str:
    """Extracts direct URLs for Slack file attachments."""
    if not files or not isinstance(files, (list, tuple)):
        return ""
    urls = []
    for item in files:
        f_obj = None
        if isinstance(item, str) and item in redux_files:
            f_obj = redux_files[item]
        elif isinstance(item, dict):
            f_obj = item
        if f_obj and isinstance(f_obj, dict):
            url = f_obj.get("permalink") or f_obj.get("url_private") or ""
            if url:
                urls.append(url)
    return " | ".join(urls)


@dataclass
class ExtractionResult:
    df: pd.DataFrame
    names: Dict[str, dict] = field(default_factory=dict)
    current_user: str = "Unknown"
    records_scanned: int = 0
    skipped_stores: int = 0


class SlackIndexedDb(idb.IndexedDb):
    """
    Subclass of Chromium IndexedDb that correctly resolves external blob references.
    
    Slack offloads large Redux payloads (>64 KB) to external .blob files. LevelDB
    retains superseded pointers until compaction. This subclass orders LevelDB
    records by sequence number ascending and verifies on-disk file existence so
    the active, live blob pointer is always resolved.
    """

    def get_blob_info(self, db_id: int, store_id: int, raw_key: bytes, file_index: int):
        cached = self._blob_lookup_cache.get((db_id, store_id, raw_key, file_index))
        if cached is not None and self._blob_dir is not None:
            blob_path = pathlib.Path(
                self._blob_dir,
                f"{db_id:x}",
                f"{cached.blob_number >> 8:02x}",
                f"{cached.blob_number:x}",
            )
            if blob_path.exists():
                return cached

        prefix = idb.IndexedDb.make_prefix(db_id, store_id, 3)
        sorted_records = sorted(self._fetched_records, key=lambda r: getattr(r, "seq", 0))

        for record in sorted_records:
            if (
                record.user_key.startswith(prefix)
                and record.state == ccl_leveldb.KeyState.Live
                and record.value
            ):
                this_raw_key = record.user_key[len(prefix):]
                buff = io.BytesIO(record.value)
                idx = 0
                while buff.tell() < len(record.value):
                    blob_info = idb.IndexedDBExternalObject.from_stream(buff)
                    if self._blob_dir is not None:
                        p = pathlib.Path(
                            self._blob_dir,
                            f"{db_id:x}",
                            f"{blob_info.blob_number >> 8:02x}",
                            f"{blob_info.blob_number:x}",
                        )
                        if p.exists():
                            self._blob_lookup_cache[(db_id, store_id, this_raw_key, idx)] = blob_info
                    else:
                        self._blob_lookup_cache[(db_id, store_id, this_raw_key, idx)] = blob_info
                    idx += 1

        if result := self._blob_lookup_cache.get((db_id, store_id, raw_key, file_index)):
            return result
        raise KeyError((db_id, store_id, raw_key, file_index))


class SlackWrappedIndexDB(idb.WrappedIndexDB):
    """WrappedIndexDB configured to use SlackIndexedDb as its raw engine."""

    def __init__(self, leveldb_dir: os.PathLike, leveldb_blob_dir: os.PathLike = None):
        self._raw_db = SlackIndexedDb(leveldb_dir, leveldb_blob_dir)
        self._multiple_origins = len(set(x.origin for x in self._raw_db.global_metadata.db_ids)) > 1
        self._db_number_lookup = {
            x.dbid_no: idb.WrappedDatabase(self._raw_db, x)
            for x in self._raw_db.global_metadata.db_ids
        }
        self._db_name_lookup = {
            (x.name, x.origin if self.has_multiple_origins else 0): x
            for x in self._db_number_lookup.values()
        }


class SlackCache:
    """Manages cache discovery, safe staging, and message extraction."""

    def __init__(self, include_dms: bool = True):
        self.include_dms = include_dms

    @staticmethod
    def _folder_last_write(folder: Path) -> float:
        times = []
        try:
            for f in folder.iterdir():
                try:
                    if f.is_file():
                        times.append(f.stat().st_mtime)
                except OSError:
                    pass
        except OSError:
            pass
        return max(times, default=0.0)

    def get_candidates(self) -> List[Tuple[Path, float]]:
        """Scans standard Windows locations and returns candidate caches sorted newest first."""
        if sys.platform != "win32":
            raise RuntimeError("SlackCache currently supports Windows only.")

        appdata = Path(os.environ.get("APPDATA", ""))
        local = Path(os.environ.get("LOCALAPPDATA", ""))

        bases = [
            appdata / "Slack",
            local / "Slack",
            *local.glob("Packages/*Slack*/LocalCache/Roaming/Slack"),
        ]

        found = []
        for base in bases:
            idb_dir = base / "IndexedDB"
            if not idb_dir.exists():
                continue

            for cache in idb_dir.glob("*.indexeddb.leveldb"):
                if "slack" in cache.name.lower():
                    found.append((cache, self._folder_last_write(cache)))

        found.sort(key=lambda x: x[1], reverse=True)
        return found

    @staticmethod
    def _stage_cache(src: Path, tmp_dir: Path) -> Tuple[Path, Optional[Path]]:
        """Copies cache to a temporary staging path, excluding active LOCK files."""
        ldb = tmp_dir / src.name
        shutil.copytree(src, ldb, ignore=shutil.ignore_patterns("LOCK"))

        blob_src = src.with_name(src.name.replace(".leveldb", ".blob"))
        blob = None
        if blob_src.exists():
            blob = tmp_dir / blob_src.name
            shutil.copytree(blob_src, blob)
        return ldb, blob

    @staticmethod
    def _resolve_person_name(d: dict) -> str:
        profile = d.get("profile") if isinstance(d.get("profile"), dict) else {}
        for key in (profile.get("real_name"), d.get("real_name"), profile.get("display_name"), d.get("name")):
            if isinstance(key, str) and key.strip():
                return key.strip()
        return ""

    def _collect_names(self, obj, names: dict, depth: int = 0):
        if depth > 40:
            return
        if isinstance(obj, dict):
            entity_id = obj.get("id")
            if isinstance(entity_id, str) and ANY_ID_RE.match(entity_id):
                prefix = entity_id[0]
                if prefix in "UWB":
                    name = self._resolve_person_name(obj)
                    if name:
                        names["people"][entity_id] = name
                elif prefix == "D":
                    if isinstance(obj.get("user"), str):
                        names["dm_with"][entity_id] = obj["user"]
                elif prefix in "CG":
                    if isinstance(obj.get("name"), str) and obj["name"]:
                        names["channels"][entity_id] = obj["name"]
                elif prefix == "T":
                    if isinstance(obj.get("name"), str) and obj["name"]:
                        names["workspace"] = {"name": obj["name"], "domain": obj.get("domain", "")}
            for v in obj.values():
                self._collect_names(v, names, depth + 1)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                self._collect_names(v, names, depth + 1)

    def _collect_messages(self, obj, channel: Optional[str] = None, depth: int = 0):
        if depth > 40:
            return
        if isinstance(obj, dict):
            ts, text = obj.get("ts"), obj.get("text")
            if isinstance(ts, str) and TS_RE.fullmatch(ts) and isinstance(text, str):
                ch = obj.get("channel") or channel
                yield {
                    "channel": ch,
                    "ts": ts,
                    "user": obj.get("user") or obj.get("bot_id"),
                    "text": text,
                    "thread_ts": obj.get("thread_ts"),
                    "rxn_key": obj.get("_rxn_key") or (f"message-{ts}-{ch}" if ch else ""),
                    "raw_reactions": obj.get("reactions"),
                    "files": obj.get("files"),
                }
            for k, v in obj.items():
                yield from self._collect_messages(
                    v, k if isinstance(k, str) and ID_RE.match(k) else channel, depth + 1
                )
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                yield from self._collect_messages(v, channel, depth + 1)

    def extract(self, cache_path: Path) -> ExtractionResult:
        """Stages and parses the specified cache, returning an ExtractionResult."""
        tmp = Path(tempfile.mkdtemp(prefix="slack_idb_"))
        rows = []
        names = {"people": {}, "channels": {}, "dm_with": {}, "workspace": {}}
        redux_reactions = {}
        redux_files = {}
        records_scanned = 0
        skipped_stores = 0
        detected_slack_user_id = None

        try:
            ldb, blob = self._stage_cache(cache_path, tmp)
            wrapper = SlackWrappedIndexDB(ldb, blob)

            for dbid in wrapper.database_ids:
                db = wrapper[dbid]
                for store_name in db.object_store_names:
                    try:
                        for record in db[store_name].iterate_records(
                            live_only=True,
                            bad_deserializer_data_handler=lambda k, d: None,
                        ):
                            records_scanned += 1
                            val = record.value
                            if val is not None:
                                key_str = str(getattr(record.key, "value", record.key))
                                match = CLIENT_KEY_RE.match(key_str)
                                if match:
                                    detected_slack_user_id = match.group(2)

                                if isinstance(val, dict):
                                    if isinstance(val.get("reactions"), dict):
                                        redux_reactions.update(val["reactions"])
                                    if isinstance(val.get("files"), dict):
                                        redux_files.update(val["files"])

                                rows.extend(self._collect_messages(val))
                                self._collect_names(val, names)
                    except Exception as e:
                        skipped_stores += 1
                        logger.warning("Database %s, store %s skipped: %s", dbid, store_name, e)
            wrapper.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        for r in rows:
            rxn_data = redux_reactions.get(r.get("rxn_key")) or r.get("raw_reactions")
            r["reactions"] = format_reactions(rxn_data, names["people"])
            r["attachments"] = format_attachments(r.get("files"), redux_files)

        df = pd.DataFrame(rows, columns=["channel", "ts", "user", "text", "thread_ts", "reactions", "attachments"])
        df = df.drop_duplicates(subset=["channel", "ts"])

        if not self.include_dms:
            df = df[~df["channel"].fillna("").str.startswith("D")]

        if not df.empty:
            df["time_utc"] = pd.to_datetime(df["ts"].astype(float), unit="s")
            df = df.sort_values(["channel", "time_utc"]).reset_index(drop=True)
        else:
            df["time_utc"] = pd.Series(dtype="datetime64[ns]")

        current_user = "Unknown"
        if detected_slack_user_id and detected_slack_user_id in names["people"]:
            current_user = names["people"][detected_slack_user_id]
        else:
            current_user = getpass.getuser()

        return ExtractionResult(
            df=df,
            names=names,
            current_user=current_user,
            records_scanned=records_scanned,
            skipped_stores=skipped_stores,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    extractor = SlackCache()
    candidates = extractor.get_candidates()
    if not candidates:
        print("No Slack cache found.")
        sys.exit(1)

    print(f"Found {len(candidates)} candidate cache(s):")
    for i, (path, mtime) in enumerate(candidates, 1):
        dt = datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else "unknown"
        print(f"[{i}] {path.name} (last write: {dt})")

    primary = candidates[0][0]
    print(f"\nExtracting from: {primary.name}...")
    res = extractor.extract(primary)
    print(f"Scanned {res.records_scanned} records ({res.skipped_stores} skipped)")
    print(f"Extracted {len(res.df)} message(s)")
    print(f"Detected Slack user: {res.current_user}")
