from time import perf_counter

from app.archive.manifest import batch_key, manifest
from app.monitor.inspection import Inspection
from app.monitor.source import input_packet


def archive_run(store, payload):
    started = perf_counter()
    files, name = input_packet(payload)
    run_id, hashes = batch_key(files)
    record = store.lookup(run_id)
    if record is not None:
        return record, True
    audit = Inspection(files)
    results = audit.results()
    packet = {'manifest': manifest(run_id, hashes, vars(audit.window), name, started),
              'originals': files, 'results': results}
    # 运行包与超限处置单在同一个 SQLite 事务内提交，失败整体回滚。
    return store.commit_run(run_id, packet)
