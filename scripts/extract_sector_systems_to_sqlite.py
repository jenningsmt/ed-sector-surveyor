"""
Extract sector systems (all bodies + rings) from a Spansh galaxy dump into SQLite.

Example usage:
  python tools/extract_sector_systems_to_sqlite.py --sector_prefix "Egnairs "
  python tools/extract_sector_systems_to_sqlite.py
"""

import argparse
import gzip
import json
import math
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

def _default_input_path() -> Path:
    env_path = os.getenv("MFI_GALAXY_DUMP")
    if env_path:
        return Path(env_path)
    repo_root = Path(__file__).resolve().parents[1]
    gz_path = repo_root / "data" / "source_data" / "galaxy.json.gz"
    if gz_path.exists():
        return gz_path
    return repo_root / "data" / "source_data" / "galaxy.json"


DEFAULT_INPUT = _default_input_path()

_override = os.getenv("MFI_SECTOR_LIBRARY_DIR")
if _override:
    DEFAULT_OUTPUT_DIR = Path(_override)
else:
    DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "sector_library"
DEFAULT_COMMIT_EVERY_SYSTEMS = 20000
DEFAULT_COMMIT_EVERY_BODIES = 100000
DEFAULT_COMMIT_EVERY_RINGS = 100000
DEFAULT_PROGRESS_SECONDS = 30
DEFAULT_PROGRESS_EVERY_SYSTEMS = 50000

# Return codes shared by the extractors (and mirrored by the GUI pipelines).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REJECTED_RECORDS = 3  # completed, but some input records were unreadable and skipped
EXIT_CANCELLED = 130


class RejectedRecordError(Exception):
    """Raised in strict mode at the first unreadable input record."""


class InputStats:
    """Counts input records that could not be used, so a tolerant run can
    report them instead of skipping them silently."""

    MAX_EXAMPLES = 5

    def __init__(self, strict: bool = False) -> None:
        self.strict = strict
        self.rejected = 0
        self.examples: List[str] = []
        self.invalid_coords = 0

    def reject(self, where: str, reason: str) -> None:
        self.rejected += 1
        if len(self.examples) < self.MAX_EXAMPLES:
            self.examples.append(f"{where}: {reason}")
        if self.strict:
            raise RejectedRecordError(f"{where}: {reason}")

    def report(self) -> None:
        if self.rejected:
            print(f"Rejected records: {self.rejected} (unreadable input, skipped)")
            for example in self.examples:
                print(f"  {example}")
        if self.invalid_coords:
            print(
                f"Systems without valid coordinates: {self.invalid_coords} "
                "(stored with empty coordinates; left out of distance searches)"
            )


def staging_path(db_path: Path) -> Path:
    """Where a fresh extraction is written until it completes."""
    return db_path.with_name(db_path.name + ".partial")


def remove_quietly(path: Path) -> None:
    for candidate in (path, path.with_name(path.name + "-journal")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def open_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def detect_format(path: Path) -> str:
    with open_text(path) as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("["):
                return "json_doc"
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    return "unknown"
                if isinstance(obj, dict):
                    return "jsonl"
            if stripped.startswith("{") and not stripped.endswith("}"):
                return "json_doc"
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return "unknown"
            if isinstance(obj, dict):
                return "jsonl"
            return "json_doc"
    return "unknown"


def safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def finite_float(value: Any) -> Optional[float]:
    """safe_float, but NaN and infinities count as missing."""
    if isinstance(value, bool):
        return None
    result = safe_float(value)
    if result is None or not math.isfinite(result):
        return None
    return result


def safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_sql_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def first_value(data: Dict[str, Any], keys: Iterable[str]) -> Optional[Any]:
    for key in keys:
        if key in data:
            value = data.get(key)
            if value is not None:
                return value
    return None


def string_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    return None


def get_system_name(system: Dict[str, Any]) -> Optional[str]:
    return string_value(first_value(system, ["name", "systemName"]))


def get_system_address(system: Dict[str, Any]) -> Optional[int]:
    value = first_value(system, ["SystemAddress", "systemAddress", "id64"])
    if isinstance(value, int):
        return value
    return safe_int(value)


def get_system_coords(system: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """All three coordinates as finite floats, or (None, None, None) if any
    is missing or invalid -- a partial position is not a usable position.
    Zero and negative values are legal."""
    coords = system.get("coords")
    source = coords if isinstance(coords, dict) else system
    xyz = tuple(finite_float(source.get(axis)) for axis in ("x", "y", "z"))
    if any(v is None for v in xyz):
        return None, None, None
    return xyz


def extract_bodies(system: Dict[str, Any]) -> List[Dict[str, Any]]:
    bodies = first_value(system, ["bodies", "Bodies"])
    if isinstance(bodies, list):
        return [body for body in bodies if isinstance(body, dict)]
    return []


def extract_rings(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    rings = first_value(body, ["rings", "Rings"])
    if isinstance(rings, list):
        return [ring for ring in rings if isinstance(ring, dict)]
    return []


def normalize_ring_class(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value.strip()


def system_key_for(system_address: Optional[int], system_name: str) -> str:
    if system_address is not None:
        return f"addr:{system_address}"
    return f"name:{system_name}"


def body_key_for(body_id: Optional[int], body_name: str) -> str:
    if body_id is not None:
        return f"id:{body_id}"
    return f"name:{body_name}"


def synth_ring_name(
    body_name: str,
    ring_class: Optional[str],
    inner_rad: Optional[float],
    outer_rad: Optional[float],
) -> str:
    ring_class_str = ring_class or "Unknown"
    inner_str = f"{inner_rad:.0f}" if inner_rad is not None else "?"
    outer_str = f"{outer_rad:.0f}" if outer_rad is not None else "?"
    return f"{body_name} {ring_class_str} {inner_str}-{outer_str}"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS systems (
            system_key TEXT PRIMARY KEY,
            system_address INTEGER NULL,
            name TEXT UNIQUE,
            x REAL NULL,
            y REAL NULL,
            z REAL NULL,
            raw_json TEXT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bodies (
            system_key TEXT NOT NULL,
            system_address INTEGER NULL,
            system_name TEXT NOT NULL,
            body_key TEXT NOT NULL,
            body_id INTEGER NULL,
            body_name TEXT NOT NULL,
            body_type TEXT NULL,
            sub_type TEXT NULL,
            terraform_state TEXT NULL,
            raw_json TEXT NULL,
            PRIMARY KEY (system_key, body_key)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rings (
            system_key TEXT NOT NULL,
            system_address INTEGER NULL,
            system_name TEXT NOT NULL,
            body_key TEXT NOT NULL,
            body_id INTEGER NULL,
            body_name TEXT NOT NULL,
            ring_name TEXT NOT NULL,
            ring_class TEXT NULL,
            mass_mt REAL NULL,
            inner_rad REAL NULL,
            outer_rad REAL NULL,
            reserve_level TEXT NULL,
            raw_json TEXT NULL,
            PRIMARY KEY (system_key, ring_name)
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_systems_name ON systems(name);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bodies_system ON bodies(system_key);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rings_system ON rings(system_key);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rings_body ON rings(body_key);")
    conn.commit()


def iter_systems_jsonl(path: Path, stats: Optional[InputStats] = None) -> Iterable[Dict[str, Any]]:
    """Yield each line's system object. Unreadable lines and non-object
    values are counted in `stats` (and raise in strict mode)."""
    stats = stats if stats is not None else InputStats()
    with open_text(path) as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                stats.reject(f"line {line_no}", f"invalid JSON ({exc.msg})")
                continue
            if isinstance(obj, dict):
                yield obj
            else:
                stats.reject(f"line {line_no}", f"not a JSON object ({type(obj).__name__})")


def iter_systems_json_doc(path: Path, stats: Optional[InputStats] = None) -> Iterable[Dict[str, Any]]:
    try:
        import ijson
    except ImportError as exc:
        raise RuntimeError("Install ijson for JSON array streaming: pip install ijson") from exc
    # yajl2_c (compiled C backend) is dramatically faster than the pure-Python
    # fallback -- on a 100+ GB dump the difference is hours. Logged so a slow
    # run can be diagnosed (e.g. a build whose ijson lacked the compiled
    # extension) without needing to inspect the environment directly.
    print(f"Detected JSON array document; streaming via ijson (backend={ijson.backend}).")
    stats = stats if stats is not None else InputStats()
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rb") as handle:
        for item_no, obj in enumerate(ijson.items(handle, "item"), start=1):
            if isinstance(obj, dict):
                yield obj
            else:
                stats.reject(f"item {item_no}", f"not a JSON object ({type(obj).__name__})")


def sanitize_prefix(prefix: str) -> str:
    trimmed = prefix.strip()
    replaced = trimmed.replace(" ", "_")
    safe = "".join(ch for ch in replaced if ch.isalnum() or ch in {"_", "-"})
    return safe or "sector"


def prompt_sector_prefix() -> str:
    print("Case-insensitive matching is ON by default in interactive mode.")
    while True:
        sys.stdout.write(
            "Enter sector/system-name prefix to extract (example: Egnairs or Eotchorts): "
        )
        sys.stdout.flush()
        value = sys.stdin.readline()
        if value is None:
            value = ""
        value = value.rstrip("\r\n")
        if value:
            return value
        print("Prefix cannot be empty.")


def matches_prefix(name: str, prefix: str, case_insensitive: bool) -> bool:
    """True if `name` starts with `prefix` on a token boundary (prefix is the
    whole name, or is followed by a space). Prevents "Oochost" from matching
    an unrelated "Oochostia XY-Z"."""
    n, p = (name.lower(), prefix.lower()) if case_insensitive else (name, prefix)
    if not n.startswith(p):
        return False
    return len(n) == len(p) or n[len(p)] == " "


def process_system(
    system: Dict[str, Any],
    sector_prefix: str,
    case_insensitive: bool,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    system_name = system.get("name") or system.get("Name") or ""
    if not system_name:
        return None, [], []

    if not matches_prefix(system_name, sector_prefix, case_insensitive):
        return None, [], []

    system_address = get_system_address(system)
    system_key = system_key_for(system_address, system_name)
    x, y, z = get_system_coords(system)

    system_row = {
        "system_key": system_key,
        "system_address": system_address,
        "name": system_name,
        "x": x,
        "y": y,
        "z": z,
        "raw_json": json.dumps(system, ensure_ascii=True, default=str),
    }

    body_rows: List[Dict[str, Any]] = []
    ring_rows: List[Dict[str, Any]] = []
    bodies = extract_bodies(system)
    for body in bodies:
        body_name = string_value(body.get("name"))
        if not body_name:
            continue
        body_id = safe_int(first_value(body, ["bodyId", "BodyID", "id"]))
        body_key = body_key_for(body_id, body_name)
        body_type = string_value(first_value(body, ["type", "bodyType", "class", "planetClass"]))
        sub_type = string_value(first_value(body, ["subType", "subtype", "subClass", "subclass"]))
        terraform_state = string_value(first_value(body, ["terraformingState", "terraformState"]))

        body_rows.append(
            {
                "system_key": system_key,
                "system_address": system_address,
                "system_name": system_name,
                "body_key": body_key,
                "body_id": body_id,
                "body_name": body_name,
                "body_type": body_type,
                "sub_type": sub_type,
                "terraform_state": terraform_state,
                "raw_json": json.dumps(body, ensure_ascii=True, default=str),
            }
        )

        for ring in extract_rings(body):
            # Spansh dumps use type/mass/innerRadius/outerRadius; the other
            # spellings are journal-style aliases.
            ring_name = string_value(first_value(ring, ["name", "Name", "ringName", "RingName"]))
            ring_class = normalize_ring_class(first_value(ring, ["type", "ringClass", "RingClass", "class"]))
            mass_mt = safe_float(first_value(ring, ["mass", "massMT", "MassMT"]))
            inner_rad = safe_float(first_value(ring, ["innerRadius", "innerRad", "InnerRad"]))
            outer_rad = safe_float(first_value(ring, ["outerRadius", "outerRad", "OuterRad"]))
            reserve_level = string_value(first_value(ring, ["reserveLevel", "ReserveLevel"]))
            if not ring_name:
                ring_name = synth_ring_name(body_name, ring_class, inner_rad, outer_rad)

            ring_rows.append(
                {
                    "system_key": system_key,
                    "system_address": system_address,
                    "system_name": system_name,
                    "body_key": body_key,
                    "body_id": body_id,
                    "body_name": body_name,
                    "ring_name": ring_name,
                    "ring_class": ring_class,
                    "mass_mt": mass_mt,
                    "inner_rad": inner_rad,
                    "outer_rad": outer_rad,
                    "reserve_level": reserve_level,
                    "raw_json": json.dumps(ring, ensure_ascii=True, default=str),
                }
            )

    return system_row, body_rows, ring_rows


def upsert_systems(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO systems (
            system_key,
            system_address,
            name,
            x,
            y,
            z,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["system_key"],
                row["system_address"],
                row["name"],
                row["x"],
                row["y"],
                row["z"],
                row["raw_json"],
            )
            for row in rows
        ],
    )


def upsert_bodies(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO bodies (
            system_key,
            system_address,
            system_name,
            body_key,
            body_id,
            body_name,
            body_type,
            sub_type,
            terraform_state,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["system_key"],
                row["system_address"],
                row["system_name"],
                row["body_key"],
                row["body_id"],
                row["body_name"],
                row["body_type"],
                row["sub_type"],
                row["terraform_state"],
                row["raw_json"],
            )
            for row in rows
        ],
    )


def upsert_rings(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO rings (
            system_key,
            system_address,
            system_name,
            body_key,
            body_id,
            body_name,
            ring_name,
            ring_class,
            mass_mt,
            inner_rad,
            outer_rad,
            reserve_level,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["system_key"],
                row["system_address"],
                row["system_name"],
                row["body_key"],
                row["body_id"],
                row["body_name"],
                row["ring_name"],
                row["ring_class"],
                row["mass_mt"],
                row["inner_rad"],
                row["outer_rad"],
                row["reserve_level"],
                row["raw_json"],
            )
            for row in rows
        ],
    )


def run(
    input_path: Path,
    sector_prefix: str,
    case_insensitive: bool = True,
    output_db: Optional[Path] = None,
    limit: Optional[int] = None,
    verbose: bool = False,
    debug_prefix: bool = False,
    stop_after_matches: int = 0,
    commit_every_systems: int = DEFAULT_COMMIT_EVERY_SYSTEMS,
    commit_every_bodies: int = DEFAULT_COMMIT_EVERY_BODIES,
    commit_every_rings: int = DEFAULT_COMMIT_EVERY_RINGS,
    progress_seconds: int = DEFAULT_PROGRESS_SECONDS,
    progress_every_systems: int = DEFAULT_PROGRESS_EVERY_SYSTEMS,
    cancel_event: Optional[threading.Event] = None,
    strict: bool = False,
) -> int:
    """Extract one sector's systems/bodies/rings into a SQLite DB.

    A new database is built under a ".partial" name and renamed into place
    only when the run completes, so a cancelled or failed run never leaves a
    half-written database under the normal name. Extracting into an existing
    database merges into it (rows are inserted or replaced; nothing is
    removed), as before.

    `limit` caps the number of input records scanned. In strict mode the run
    stops at the first unreadable record; otherwise such records are skipped,
    counted and reported.

    Return codes: EXIT_OK (0); EXIT_FAILED (1) for input/format errors, write
    failures and strict-mode rejections; EXIT_REJECTED_RECORDS (3) when the
    run completed but skipped unreadable records; EXIT_CANCELLED (130) when
    interrupted (KeyboardInterrupt or cancel_event set).
    """
    sector_prefix = sector_prefix.rstrip("\r\n")

    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return 1

    if not output_db:
        sanitized = sanitize_prefix(sector_prefix)
        output_db = DEFAULT_OUTPUT_DIR / f"sector_{sanitized}.sqlite"

    db_path = Path(output_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    parsed_format = detect_format(input_path)
    if parsed_format == "unknown":
        print("Unable to detect galaxy.json format (jsonl vs json document).")
        return EXIT_FAILED

    stats = InputStats(strict=strict)
    fresh = not db_path.exists()
    write_path = staging_path(db_path) if fresh else db_path
    if fresh:
        remove_quietly(write_path)  # leftover from an earlier failed run
    conn = sqlite3.connect(write_path)
    try:
        init_db(conn)
    except Exception:
        conn.close()
        raise

    systems_scanned = 0
    systems_written = 0
    bodies_written = 0
    rings_written = 0
    systems_with_rings = 0
    matched_systems = 0

    system_rows: List[Dict[str, Any]] = []
    body_rows: List[Dict[str, Any]] = []
    ring_rows: List[Dict[str, Any]] = []

    start_time = time.monotonic()
    last_progress_time = start_time
    last_progress_systems = 0

    def flush() -> None:
        nonlocal systems_written, bodies_written, rings_written
        if not system_rows and not body_rows and not ring_rows:
            return
        conn.execute("BEGIN")
        upsert_systems(conn, system_rows)
        upsert_bodies(conn, body_rows)
        upsert_rings(conn, ring_rows)
        conn.commit()
        systems_written += len(system_rows)
        bodies_written += len(body_rows)
        rings_written += len(ring_rows)
        system_rows.clear()
        body_rows.clear()
        ring_rows.clear()

    def print_progress(force: bool = False) -> None:
        nonlocal last_progress_time, last_progress_systems
        now = time.monotonic()
        elapsed = now - start_time
        if not force:
            if elapsed <= 0:
                return
            if (now - last_progress_time) < progress_seconds and (
                systems_scanned - last_progress_systems
            ) < progress_every_systems:
                return
        last_progress_time = now
        last_progress_systems = systems_scanned
        elapsed_minutes = elapsed / 60.0 if elapsed > 0 else 0.0
        rate = systems_scanned / elapsed if elapsed > 0 else 0.0
        pending_systems = len(system_rows)
        pending_bodies = len(body_rows)
        pending_rings = len(ring_rows)
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        print(
            (
                f"{timestamp} scanned={systems_scanned} systems_written={systems_written} "
                f"bodies_written={bodies_written} rings_written={rings_written} "
                f"pending_systems={pending_systems} pending_bodies={pending_bodies} "
                f"pending_rings={pending_rings} elapsed_min={elapsed_minutes:.2f} "
                f"rate={rate:.2f}/s"
            ),
            flush=True,
        )

    debug_names_seen = 0
    debug_match_seen = 0
    interrupted = False
    failure: Optional[str] = None
    try:
        if parsed_format == "jsonl":
            iterator = iter_systems_jsonl(input_path, stats)
        else:
            iterator = iter_systems_json_doc(input_path, stats)

        for system in iterator:
            if cancel_event is not None and cancel_event.is_set():
                interrupted = True
                print("Cancelled by user; flushing pending data...", flush=True)
                break

            if limit is not None and systems_scanned >= limit:
                break
            systems_scanned += 1

            if not isinstance(system, dict):
                print_progress()
                continue

            if debug_prefix and debug_names_seen < 5:
                name_preview = system.get("name") or system.get("Name") or ""
                print(f"debug system_name[{debug_names_seen}]: {name_preview!r}")
                debug_names_seen += 1
                if debug_names_seen == 1:
                    print(f"debug sector_prefix: {sector_prefix!r}")
                    print(f"debug case_insensitive: {case_insensitive}")

            system_row, body_list, ring_list = process_system(
                system, sector_prefix, case_insensitive
            )
            if debug_prefix and debug_match_seen < 5:
                name_preview = system.get("name") or system.get("Name") or ""
                matched = system_row is not None
                print(f"debug match[{debug_match_seen}]: {name_preview!r} -> {matched}")
                debug_match_seen += 1
            if system_row is None:
                print_progress()
                continue

            matched_systems += 1
            systems_with_rings += 1 if ring_list else 0
            if system_row["x"] is None:
                stats.invalid_coords += 1
            system_rows.append(system_row)
            body_rows.extend(body_list)
            ring_rows.extend(ring_list)

            if (
                len(system_rows) >= commit_every_systems
                or len(body_rows) >= commit_every_bodies
                or len(ring_rows) >= commit_every_rings
            ):
                flush()

            if verbose:
                print_progress()

            if debug_prefix and systems_scanned % 1_000_000 == 0:
                name_preview = system.get("name") or system.get("Name") or ""
                print(
                    f"debug sample@{systems_scanned}: {name_preview!r} match_count={matched_systems}"
                )

            if stop_after_matches and matched_systems >= stop_after_matches:
                break

        flush()
    except KeyboardInterrupt:
        interrupted = True
        print("KeyboardInterrupt received; flushing pending data...", flush=True)
        try:
            flush()
        except Exception:
            conn.rollback()
    except RejectedRecordError as exc:
        failure = f"Stopped at the first unreadable record (strict mode): {exc}"
    except Exception as exc:
        failure = f"Extraction failed: {type(exc).__name__}: {exc}"
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    elapsed = time.monotonic() - start_time
    elapsed_minutes = elapsed / 60.0 if elapsed > 0 else 0.0
    rate = systems_scanned / elapsed if elapsed > 0 else 0.0

    print(f"Systems scanned: {systems_scanned}")
    print(f"Systems inserted: {systems_written}")
    print(f"Bodies inserted: {bodies_written}")
    print(f"Rings inserted: {rings_written}")
    print(f"Systems with >=1 ring: {systems_with_rings}")
    print(f"Elapsed minutes: {elapsed_minutes:.2f}")
    print(f"Average rate: {rate:.2f} systems/sec")
    stats.report()

    if failure or interrupted:
        if fresh:
            remove_quietly(write_path)
            print(f"No database written: {db_path} was not created.")
        else:
            print(f"Existing database kept: {db_path} (it may hold part of this run's updates).")
        if failure:
            print(failure)
            return EXIT_FAILED
        return EXIT_CANCELLED

    if fresh:
        os.replace(write_path, db_path)
    print(f"Database written to: {db_path}")
    return EXIT_REJECTED_RECORDS if stats.rejected else EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract sector systems from a Spansh galaxy dump into SQLite."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output_db", default=None)
    parser.add_argument("--sector_prefix", default=None)
    parser.add_argument("--case_insensitive", action="store_true")
    parser.add_argument("--case_sensitive", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Stop after scanning this many input records")
    parser.add_argument(
        "--strict", action="store_true",
        help="Stop at the first unreadable input record instead of skipping and reporting it",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug_prefix", action="store_true")
    parser.add_argument("--stop_after_matches", type=int, default=0)
    parser.add_argument("--commit-every-systems", type=int, default=DEFAULT_COMMIT_EVERY_SYSTEMS)
    parser.add_argument("--commit-every-bodies", type=int, default=DEFAULT_COMMIT_EVERY_BODIES)
    parser.add_argument("--commit-every-rings", type=int, default=DEFAULT_COMMIT_EVERY_RINGS)
    parser.add_argument("--progress-seconds", type=int, default=DEFAULT_PROGRESS_SECONDS)
    parser.add_argument("--progress-every-systems", type=int, default=DEFAULT_PROGRESS_EVERY_SYSTEMS)
    args = parser.parse_args()

    if args.case_sensitive and args.case_insensitive:
        print("Choose only one of --case_insensitive or --case_sensitive.")
        return 1

    sector_prefix = args.sector_prefix
    interactive_prompt = False
    if not sector_prefix:
        interactive_prompt = True
        sector_prefix = prompt_sector_prefix()

    if interactive_prompt:
        case_insensitive = not args.case_sensitive
    else:
        case_insensitive = args.case_insensitive and not args.case_sensitive

    return run(
        input_path=Path(args.input),
        sector_prefix=sector_prefix,
        case_insensitive=case_insensitive,
        output_db=Path(args.output_db) if args.output_db else None,
        limit=args.limit,
        verbose=args.verbose,
        debug_prefix=args.debug_prefix,
        stop_after_matches=args.stop_after_matches,
        commit_every_systems=args.commit_every_systems,
        commit_every_bodies=args.commit_every_bodies,
        commit_every_rings=args.commit_every_rings,
        progress_seconds=args.progress_seconds,
        progress_every_systems=args.progress_every_systems,
        strict=args.strict,
    )


if __name__ == "__main__":
    sys.exit(main())
