import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from app.archive.handoff import reconcile
from app.archive.manifest import canonical
from app.errors import InputError


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS monitoring (id TEXT PRIMARY KEY, packet TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS handoffs (
                scope_key TEXT NOT NULL, event_id TEXT NOT NULL, ticket TEXT NOT NULL,
                PRIMARY KEY (scope_key, event_id))''')

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            yield db

    @contextmanager
    def write_transaction(self):
        """立即拿写锁，使两班并发提交同原件包时严格串行、只建一张处置单。"""
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def lookup(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT packet FROM monitoring WHERE id = ?', (run_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def require(self, run_id):
        packet = self.lookup(run_id)
        if packet is None:
            raise InputError('RUN_UNKNOWN', '找不到监测留档', 404)
        return packet

    def commit_run(self, run_id, packet):
        """归档运行包并在同一事务对账处置单；重复提交复用原归档。"""
        events = packet['results']['exceedance']['events']
        with self.write_transaction() as db:
            existed = db.execute('SELECT 1 FROM monitoring WHERE id = ?', (run_id,)).fetchone()
            db.execute(
                'INSERT INTO monitoring VALUES (?, ?) ON CONFLICT(id) DO NOTHING',
                (run_id, canonical(packet)))
            if existed is not None:
                # 并发的另一班已提交同一原件包：复用既有运行与处置单，不再重复建单。
                record = db.execute('SELECT packet FROM monitoring WHERE id = ?',
                                    (run_id,)).fetchone()[0]
            else:
                enriched, tickets = reconcile(db, run_id, packet, events)
                packet['results']['exceedance']['events'] = enriched
                packet['results']['exceedance']['tickets'] = tickets
                db.execute('UPDATE monitoring SET packet = ? WHERE id = ?',
                           (canonical(packet), run_id))
                record = db.execute('SELECT packet FROM monitoring WHERE id = ?',
                                    (run_id,)).fetchone()[0]
        return json.loads(record), existed is not None

    def handoffs(self, scope_key):
        with self.connection() as db:
            rows = db.execute('SELECT ticket FROM handoffs WHERE scope_key = ? ORDER BY rowid',
                              (scope_key,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def history(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT json_extract(packet, '$.manifest') FROM monitoring ORDER BY rowid DESC LIMIT 100")]
