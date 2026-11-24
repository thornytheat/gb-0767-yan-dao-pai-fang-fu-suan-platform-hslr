"""处置单交接生命周期：补证确证/撤销、两班同单、撤单留痕、归档事务与历史查阅。"""

import json
from datetime import UTC, datetime, timedelta

from app.archive.run import archive_run
from app.archive.store import Archive
from app.monitor.source import sample

START = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)


def _window_json(limit='50'):
    return json.dumps({
        'stack': 'K-04', 'start': '2026-11-14T08:00+08:00', 'end': '2026-11-14T02:00Z',
        'cutoff': '2026-11-14T04:00Z', 'reference': '10', 'threshold': 45,
        'tolerance': '0.50', 'limit': limit})


HEADER = 'stack,timestamp,value,unit,basis,moisture,oxygen,status,revision,received'


def _minute(offset, value, status='OK', revision=1):
    when = (START + timedelta(minutes=offset)).isoformat().replace('+00:00', 'Z')
    received = (START + timedelta(minutes=offset + 1)).isoformat().replace('+00:00', 'Z')
    return f'K-04,{when},{value},mg/Nm3,dry,-,10,{status},{revision},{received}'


def _packets(rows):
    base = sample('clean')
    minutes = '\n'.join([HEADER] + [_minute(offset, *spec) for offset, spec in enumerate(rows)])
    return base | {'window': _window_json(), 'minutes': minutes,
                   'modes': 'stack,start,end,mode', 'reports': 'stack,hour,value,ticket'}


# 每个元素为 (value, status)；缺测行直接不给读数，用状态行占位由上层另行构造。
def _high(n):
    return [('60', 'OK')] * n


def _low(n):
    return [('30', 'OK')] * n


def _packet_with_gap(pattern, gap_offsets):
    """pattern 给出每分钟 (value,status)，gap_offsets 中的分钟改为缺测（NULL）。"""
    rows = []
    for offset, (value, status) in enumerate(pattern):
        rows.append(('NULL', 'OK') if offset in gap_offsets else (value, status))
    return _packets(rows)


def test_pending_then_confirmed_after_late_record_fills_gap(client):
    pattern = _high(10) + [('60', 'OK')] + _high(9) + _low(100)
    first = _packet_with_gap(pattern, {10})
    run1 = client.post('/api/runs', json={'files': first}).json
    event1 = _exceedance(client, run1['run_id'])['events'][0]
    assert event1['status'] == 'PENDING'
    ticket_no = event1['ticket_no']

    # 晚到记录补齐第 10 分钟：19+1 个连续高值达到 15 分钟，同一张单转确证。
    run2 = client.post('/api/runs', json={'files': _packets(pattern)}).json
    assert run2['run_id'] != run1['run_id']
    event2 = _exceedance(client, run2['run_id'])['events'][0]
    assert (event2['status'], event2['ticket_no']) == ('CONFIRMED', ticket_no)
    assert [step['status'] for step in event2['history']] == ['PENDING', 'CONFIRMED']

    tickets = client.get(f"/api/runs/{run2['run_id']}/handoffs").json['tickets']
    assert len(tickets) == 1 and tickets[0]['ticket_no'] == ticket_no


def test_pending_revoked_when_filled_record_breaks_high_run(client):
    pattern = _high(10) + [('30', 'OK')] + _high(9) + _low(100)
    first = _packet_with_gap(pattern, {10})
    run1 = client.post('/api/runs', json={'files': first}).json
    assert _exceedance(client, run1['run_id'])['events'][0]['status'] == 'PENDING'

    run2 = client.post('/api/runs', json={'files': _packets(pattern)}).json
    events = _exceedance(client, run2['run_id'])['events']
    assert events == []  # 补齐后两段都不足 15 分钟，无事件
    ticket = client.get(f"/api/runs/{run2['run_id']}/handoffs").json['tickets'][0]
    assert ticket['status'] == 'REVOKED'
    assert [step['status'] for step in ticket['history']] == ['PENDING', 'REVOKED']


def test_two_shifts_submitting_same_window_keep_single_ticket(client):
    files = _packets(_high(16) + _low(104))
    first = client.post('/api/runs', json={'files': files}).json
    duplicate = client.post('/api/runs', json={'files': files}).json
    assert duplicate['reused'] is True  # 同原件包复用运行编号
    scope = f"K-04|{first['manifest']['parameters']['start']}"
    tickets = Archive(_db_path(client)).handoffs(scope)
    assert len(tickets) == 1
    assert tickets[0]['status'] == 'CONFIRMED'


def test_event_disappearance_withdraws_ticket_but_keeps_history(client):
    high_files = _packets(_high(16) + _low(104))
    run1 = client.post('/api/runs', json={'files': high_files}).json
    low_files = _packets(_low(120))
    run2 = client.post('/api/runs', json={'files': low_files}).json
    ticket = client.get(f"/api/runs/{run2['run_id']}/handoffs").json['tickets'][0]
    assert ticket['status'] == 'WITHDRAWN'
    assert [step['status'] for step in ticket['history']] == ['CONFIRMED', 'WITHDRAWN']
    # 已归档的首次结论仍按当时证据查阅。
    archived = _exceedance(client, run1['run_id'])
    assert archived['events'][0]['ticket_status'] == 'CONFIRMED'


def test_hour_page_hands_off_pending_event(client):
    files = _packet_with_gap(_high(12) + _low(108), {5})
    run_id = client.post('/api/runs', json={'files': files}).json['run_id']
    page = client.get(f'/monitor/hours?run={run_id}').get_data(as_text=True)
    assert '超限事件交接' in page and '待研判（缺测未补）' in page
    assert '当前处置单（含历史结论）' in page


def test_exemption_breaks_chain_in_pipeline(client):
    # 00:00–00:09 高值，00:10–00:11 停机免计，00:12–00:26 连续 15 个高值。
    pattern = _high(27) + _low(93)
    files = _packets(pattern)
    files['modes'] = ('stack,start,end,mode\n'
                      'K-04,2026-11-14T00:10Z,2026-11-14T00:12Z,SHUTDOWN')
    run_id = client.post('/api/runs', json={'files': files}).json['run_id']
    events = _exceedance(client, run_id)['events']
    assert len(events) == 1 and events[0]['status'] == 'CONFIRMED'
    # 前 10 分钟被免计断开不与后段接续；后段 00:12 起 15 个连续高值独立确证。
    assert events[0]['high_minutes'] == 15
    assert events[0]['start'] == '2026-11-14T00:12:00+00:00'


def test_rollback_leaves_no_ticket_on_bad_packet(tmp_path):
    store = Archive(tmp_path / 'roll.sqlite')
    corrupt = sample('dirty') | {'reports': 'bad,header'}
    import pytest
    from app.errors import InputError
    with pytest.raises(InputError):
        archive_run(store, {'files': corrupt})
    assert store.handoffs('K-04|2026-11-14T00:00:00+00:00') == []


def test_handoffs_endpoint_and_certificate_carry_event(client):
    files = _packets(_high(16) + _low(104))
    run_id = client.post('/api/runs', json={'files': files}).json['run_id']
    tickets = client.get(f'/api/runs/{run_id}/handoffs').json['tickets']
    assert len(tickets) == 1 and tickets[0]['status'] == 'CONFIRMED'
    # 证据单包含冻结当时的超限事件快照。
    exported = client.get(f'/api/export/{run_id}').json
    event = exported['results']['exceedance']['events'][0]
    assert (event['ticket_status'], event['high_minutes']) == ('CONFIRMED', 16)
    # 未知运行的处置单查询返回 422 风格的 404 业务错误。
    assert client.get('/api/runs/missing/handoffs').status_code == 404


def test_preview_exceedance_stage_has_no_ticket_fields(client):
    payload = client.get('/api/samples/dirty/exceedance').json
    assert payload['limit_mg_nm3'] == '50'
    assert all('ticket_no' not in event for event in payload['events'])


# --- helpers -------------------------------------------------------------

def _db_path(client):
    return client.application.extensions['monitor_archive'].path


def _exceedance(client, run_id):
    return client.get(f'/api/runs/{run_id}/exceedance').json
