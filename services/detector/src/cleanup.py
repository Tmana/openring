"""Data retention — periodically prune old snapshots, events, visits, and metrics."""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DAILY_SECONDS = 24 * 3600


class RetentionCleaner:
    """Deletes data older than *retention_days* on a daily cycle.

    Prunes: snapshot files on disk, video clips on disk (v0.2),
    detection_events (unlabeled only), visit_sessions, and system_metrics
    rows.  Labeled events (training data) and _system events (arm/disarm
    audit trail) are never pruned.  v0.2 clips inherit the same rule —
    a labelled event's clip is preserved indefinitely.

    Runs once at startup then every 24 hours as a daemon thread.  Setting
    *retention_days* to 0 or a negative value disables all cleanup.
    """

    def __init__(
        self,
        snapshot_dir: str,
        db_path: str,
        retention_days: int = 90,
        clips_dir: str | None = None,
    ) -> None:
        self._snapshot_dir = Path(snapshot_dir)
        self._db_path = db_path
        self.retention_days = retention_days
        # v0.2: clips_dir is optional — None disables clip pruning so older
        # detector deployments without a clipper sidecar don't blow up.
        self._clips_dir = Path(clips_dir) if clips_dir else None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="retention-cleanup", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("Retention cleaner started — retention=%d days", self.retention_days)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        self._run()
        while not self._stop.wait(timeout=_DAILY_SECONDS):
            self._run()

    def _run(self) -> None:
        if self.retention_days <= 0:
            logger.debug("Data retention disabled (retention_days=%d)", self.retention_days)
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        cutoff_iso = cutoff.isoformat()
        logger.info("Retention cleanup — cutoff=%s", cutoff.date().isoformat())

        self._prune_snapshots(cutoff)
        if self._clips_dir is not None:
            self._prune_clips(cutoff)
        self._prune_db(cutoff_iso)

    def _prune_snapshots(self, cutoff: datetime) -> None:
        """Delete snapshot files older than cutoff and NULL their DB paths."""
        deleted: list[str] = []
        try:
            for f in self._snapshot_dir.iterdir():
                if not f.is_file():
                    continue
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    try:
                        f.unlink()
                        deleted.append(str(f))
                        logger.debug("Deleted snapshot: %s", f.name)
                    except OSError:
                        logger.warning("Could not delete snapshot %s", f, exc_info=True)
        except OSError:
            logger.warning("Could not iterate snapshot directory %s", self._snapshot_dir, exc_info=True)
            return

        if deleted:
            self._clear_db_paths(deleted)
            logger.info("Snapshots pruned: %d file(s)", len(deleted))
        else:
            logger.info("Snapshots: nothing to prune")

    def _prune_clips(self, cutoff: datetime) -> None:
        """Delete clip MP4s older than cutoff, preserving labelled-event clips.

        Per ROADMAP #17: ``Labelled events keep their clips indefinitely.``
        Clip filenames are ``<feedback_token>.mp4``, so we can map a clip
        file back to a detection_events row via the token without joining
        the clips table itself (which lives in the same DB but is owned
        by the clipper sidecar).

        Three-step protocol:
          1. Enumerate clip files older than cutoff.
          2. Filter out those whose feedback_token has feedback IS NOT NULL
             — those are training data and stay forever.
          3. Delete the surviving files + their rows in the clips table.
        """
        clips_dir = self._clips_dir
        if clips_dir is None or not clips_dir.is_dir():
            return

        candidates: list[tuple[Path, str]] = []
        try:
            for f in clips_dir.iterdir():
                if not f.is_file() or f.suffix != ".mp4":
                    continue
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if mtime >= cutoff:
                    continue
                # Filename convention: <feedback_token>.mp4
                token = f.stem
                if not token:
                    continue
                candidates.append((f, token))
        except OSError:
            logger.warning("Could not iterate clips directory %s", clips_dir, exc_info=True)
            return

        if not candidates:
            logger.info("Clips: nothing to prune")
            return

        # Filter out labelled events.
        labelled_tokens = self._labelled_tokens([t for _, t in candidates])
        deletable = [(f, t) for f, t in candidates if t not in labelled_tokens]
        kept_for_label = len(candidates) - len(deletable)

        deleted_files: list[str] = []
        deleted_tokens: list[str] = []
        for f, token in deletable:
            try:
                f.unlink()
                deleted_files.append(str(f))
                deleted_tokens.append(token)
                logger.debug("Deleted clip: %s", f.name)
            except OSError:
                logger.warning("Could not delete clip %s", f, exc_info=True)

        if deleted_tokens:
            self._delete_clip_rows(deleted_tokens)

        if deleted_files or kept_for_label:
            logger.info(
                "Clips pruned: %d file(s) removed, %d kept (labelled events)",
                len(deleted_files), kept_for_label,
            )
        else:
            logger.info("Clips: nothing to prune")

    def _labelled_tokens(self, tokens: list[str]) -> set[str]:
        """Of the given feedback_tokens, return those whose detection_events
        row carries non-NULL feedback — i.e. training data we mustn't
        prune.  Errors fall back to "treat as unlabelled" rather than
        silently keep everything (which would gradually fill the disk).
        """
        if not tokens:
            return set()
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                placeholders = ",".join("?" * len(tokens))
                rows = conn.execute(
                    f"SELECT feedback_token FROM detection_events"
                    f" WHERE feedback IS NOT NULL"
                    f"   AND feedback_token IN ({placeholders})",
                    tokens,
                ).fetchall()
                return {r[0] for r in rows}
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to look up labelled tokens", exc_info=True)
            return set()

    def _delete_clip_rows(self, tokens: list[str]) -> None:
        """Drop clips-table rows for the deleted clip files.  Clipper owns
        the table; we only DELETE here, never INSERT/UPDATE."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                placeholders = ",".join("?" * len(tokens))
                # Table may not exist yet (clipper never ran on this host) —
                # swallow the OperationalError quietly.
                try:
                    conn.execute(
                        f"DELETE FROM clips WHERE feedback_token IN ({placeholders})",
                        tokens,
                    )
                    conn.commit()
                except sqlite3.OperationalError:
                    logger.debug("clips table missing — skipping row delete")
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to delete clips rows", exc_info=True)

    def _clear_db_paths(self, paths: list[str]) -> None:
        """Set snapshot_path = NULL for any DB rows whose file was deleted."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                placeholders = ",".join("?" * len(paths))
                conn.execute(
                    f"UPDATE detection_events SET snapshot_path = NULL"
                    f" WHERE snapshot_path IN ({placeholders})",
                    paths,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to clear snapshot paths in database", exc_info=True)

    def _prune_db(self, cutoff_iso: str) -> None:
        """Prune old events, visits, and metrics from the database."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")

                # Events: only unlabeled, non-system events
                cur = conn.execute(
                    "DELETE FROM detection_events"
                    " WHERE timestamp < ?"
                    " AND feedback IS NULL"
                    " AND camera_name != '_system'",
                    (cutoff_iso,),
                )
                events_deleted = cur.rowcount

                # Visit sessions
                cur = conn.execute(
                    "DELETE FROM visit_sessions WHERE start_time < ?",
                    (cutoff_iso,),
                )
                visits_deleted = cur.rowcount

                # System metrics
                cur = conn.execute(
                    "DELETE FROM system_metrics WHERE timestamp < ?",
                    (cutoff_iso,),
                )
                metrics_deleted = cur.rowcount

                conn.commit()

                if events_deleted or visits_deleted or metrics_deleted:
                    logger.info(
                        "DB pruned: %d event(s), %d visit(s), %d metric sample(s)",
                        events_deleted,
                        visits_deleted,
                        metrics_deleted,
                    )
                else:
                    logger.info("DB: nothing to prune")
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to prune database records", exc_info=True)
