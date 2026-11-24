"""超限处置单注册表与交接对账。

处置单按事件业务键（烟道 + 窗口起点 + 首高值分钟）登记。两个班次对同一时段同时提交
补证时，归档去重使同一原件包只产生一个运行编号；处置单再以事件键做唯一约束，因此同一
事件始终只保留一张当前处置单，重复提交走 UPSERT 而非新增。

每次归档复算时，注册表与运行包在同一个 SQLite 事务内更新：

- 新事件建立处置单，状态沿用本次证据（PENDING/CONFIRMED）。
- 晚到监测记录补齐缺测后，原 PENDING 单随新证据转为 CONFIRMED 或 REVOKED。
- 本次证据中不再出现的 PENDING 事件转 REVOKED；已确证事件消失则转 WITHDRAWN。
  历史快照原样保留，已归档结论仍可按当时的证据查阅。
"""

import json
from datetime import UTC, datetime

from app.archive.manifest import canonical

ACTIVE_STATES = ('PENDING', 'CONFIRMED')


def reconcile(db, run_id, packet, events):
    """在调用方事务连接内对账处置单，返回带处置状态的事件与处置单清单。"""
    scope = packet['manifest']['parameters']
    scope_key = f"{scope['stack']}|{scope['start']}"
    now = datetime.now(UTC).isoformat(timespec='seconds')
    tickets = {row[0]: json.loads(row[1]) for row in db.execute(
        'SELECT event_id, ticket FROM handoffs WHERE scope_key = ?', (scope_key,))}

    current_keys = {event['event_id'] for event in events}
    enriched = []
    for event in events:
        previous = tickets.get(event['event_id'])
        ticket = _build_ticket(previous, event, run_id, now)
        tickets[event['event_id']] = ticket
        enriched.append(event | {'ticket_no': ticket['ticket_no'], 'ticket_status': ticket['status'],
                                 'history': ticket['history']})

    for event_id, ticket in tickets.items():
        if event_id in current_keys or ticket['status'] not in ACTIVE_STATES:
            continue
        # 同一事件仍在本次证据中但已低于判据（如 PENDING 补齐后高值不再连续）：撤销；
        # 事件整体消失（段被免计断开或读数回落）：撤单，历史结论均保留可查。
        next_status = 'REVOKED' if ticket['status'] == 'PENDING' else 'WITHDRAWN'
        tickets[event_id] = dict(ticket) | {
            'status': next_status,
            'history': ticket['history'] + [
                {'status': next_status, 'run_id': run_id, 'at': now,
                 'note': _transition_note(ticket['status'], next_status)}]}

    for event_id, ticket in tickets.items():
        db.execute(
            '''INSERT INTO handoffs (scope_key, event_id, ticket) VALUES (?, ?, ?)
               ON CONFLICT(scope_key, event_id) DO UPDATE SET ticket = excluded.ticket''',
            (scope_key, event_id, canonical(ticket)))
    return enriched, sorted(tickets.values(), key=lambda item: item['opened_at'])


def _build_ticket(previous, event, run_id, now):
    if previous is None:
        ticket_no = f'H-{event["start"][:10].replace("-", "")}-{event["event_id"][-8:]}'
        note = ('缺测分钟未补齐，先交接值班员排查，不得直接判超限'
                if event['status'] == 'PENDING'
                else '连续有效高值分钟达到判据，超限确证')
        return {'ticket_no': ticket_no, 'event_id': event['event_id'], 'status': event['status'],
                'opened_run': run_id, 'opened_at': now, 'current_run': run_id,
                'history': [{'status': event['status'], 'run_id': run_id, 'at': now, 'note': note}]}
    ticket = dict(previous)
    ticket['current_run'] = run_id
    if previous['status'] != event['status']:
        ticket['status'] = event['status']
        ticket['history'] = previous['history'] + [
            {'status': event['status'], 'run_id': run_id, 'at': now,
             'note': _transition_note(previous['status'], event['status'])}]
    return ticket


def _transition_note(old, new):
    if old == 'PENDING' and new == 'CONFIRMED':
        return '晚到记录补齐缺测，连续高值达到十五分钟，事件转为超限确证'
    if old == 'PENDING' and new == 'REVOKED':
        return '晚到记录补齐后高值不再连续或低于限值，事件撤销'
    if new == 'WITHDRAWN':
        return '本次复算证据中事件消失，当前处置单撤回，历史结论保留'
    return f'处置状态由 {old} 变更为 {new}'
