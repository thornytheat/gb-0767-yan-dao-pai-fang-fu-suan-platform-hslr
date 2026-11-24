from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.monitor.exceedance import CONFIRM_MINUTES, event_key, scan_events

START = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
LIMIT = Decimal('50')


def window(limit=LIMIT):
    from app.monitor.schema import Window
    return Window(stack='K-04', start=START, end=START + timedelta(hours=2),
                  cutoff=START + timedelta(hours=4), reference=Decimal('10'),
                  threshold=45, tolerance=Decimal('0.5'), limit=limit)


def row(offset, state='valid', value=None, code=None):
    when = START + timedelta(minutes=offset)
    return {'utc': when.isoformat(), 'local': when.strftime('%m-%d %H:%M'), 'state': state,
            'code': code or ('VALID' if state == 'valid' else
                             'EXEMPT' if state == 'exempt' else 'NO_READING'),
            'value': value, 'late': False}


def high(offset, amount='60'):
    return row(offset, value=Decimal(amount))


def low(offset, amount='30'):
    return row(offset, value=Decimal(amount))


def gap(offset, code='NO_READING'):
    return row(offset, state='missing', code=code)


def exempt(offset):
    return row(offset, state='exempt')


def test_fifteen_continuous_high_minutes_confirmed():
    minutes = [high(i) for i in range(15)] + [low(i) for i in range(15, 60)]
    events = scan_events(minutes, window())
    assert len(events) == 1
    event = events[0]
    assert (event['status'], event['high_minutes'], event['gap_minutes']) == ('CONFIRMED', 15, 0)
    assert event['start'] == high(0)['utc'] and event['end'] == high(14)['utc']


def test_strictly_above_limit():
    # 等于限值不算高值；只有严格高于才计数。
    minutes = [row(i, value=LIMIT) for i in range(20)]
    assert scan_events(minutes, window()) == []


def test_fourteen_high_without_gap_emits_no_ticket():
    minutes = [high(i) for i in range(14)] + [low(i) for i in range(14, 60)]
    assert scan_events(minutes, window()) == []


def test_single_missing_minute_between_high_values_is_pending_not_confirmed():
    # 两段高值中间缺一分钟：不得直接判超限，挂 PENDING 交接排查。
    minutes = [high(i) for i in range(10)] + [gap(10)] + [high(i) for i in range(11, 20)]
    events = scan_events(minutes, window())
    assert len(events) == 1
    event = events[0]
    assert event['status'] == 'PENDING'
    assert (event['high_minutes'], event['gap_minutes']) == (19, 1)
    assert event['gaps'][0]['utc'] == gap(10)['utc']


def test_shutdown_exemption_breaks_the_chain():
    # 停机免计分钟硬断开：两侧高值各自成段，缺口/接续不得跨过免计分钟。
    minutes = ([high(i) for i in range(10)] + [exempt(10), exempt(11)]
               + [high(i) for i in range(12, 27)])
    events = scan_events(minutes, window())
    assert [event['status'] for event in events] == ['CONFIRMED']
    event, = events
    assert (event['start'], event['high_minutes']) == (high(12)['utc'], 15)


def test_high_run_carries_across_the_hour_boundary():
    # 跨整点的高值分钟接续：高值从第 55 分钟延续到下一小时第 9 分钟，共 15 分钟。
    minutes = [low(i) for i in range(55)] + [high(i) for i in range(55, 70)]
    minutes += [low(i) for i in range(70, 120)]
    events = scan_events(minutes, window())
    assert len(events) == 1
    assert (events[0]['status'], events[0]['high_minutes']) == ('CONFIRMED', 15)
    assert events[0]['start'] == high(55)['utc']


def test_normal_valid_low_value_also_breaks_chain():
    minutes = [high(i) for i in range(10)] + [low(10)] + [high(i) for i in range(11, 26)]
    events = scan_events(minutes, window())
    assert len(events) == 1
    assert events[0]['status'] == 'CONFIRMED'
    assert events[0]['start'] == high(11)['utc']


def test_trailing_gap_keeps_event_pending_awaiting_late_record():
    # 高值段以缺测收尾：晚到记录可能补成超限，仍以 PENDING 交接而非直接撤销。
    minutes = [high(i) for i in range(5)] + [gap(5)] + [low(i) for i in range(6, 60)]
    events = scan_events(minutes, window())
    assert len(events) == 1
    assert (events[0]['status'], events[0]['gap_minutes']) == ('PENDING', 1)


def test_short_break_then_gap_without_high_emits_nothing():
    # 短高值先被正常低值断开，之后的缺测不再属于任何高值段，不出单。
    minutes = [high(i) for i in range(5)] + [low(5), gap(6)] + [low(i) for i in range(7, 60)]
    assert scan_events(minutes, window()) == []


def test_event_key_stable_on_rescan():
    minutes = [high(i) for i in range(15)]
    event = scan_events(minutes, window())[0]
    assert event['event_id'] == event_key(window(), high(0)['utc'])
    # 同窗口重扫，事件键保持稳定，处置单才能接续。
    assert scan_events(minutes, window())[0]['event_id'] == event['event_id']


def test_confirm_minutes_constant_is_fifteen():
    assert CONFIRM_MINUTES == 15


def test_window_requires_positive_limit():
    import pytest
    from app.errors import InputError
    from app.monitor.schema import Window
    base = {'stack': 'K-04', 'start': '2026-11-14T08:00+08:00', 'end': '2026-11-14T02:00Z',
            'cutoff': '2026-11-14T04:00Z', 'reference': '10', 'threshold': 45, 'tolerance': '0.5'}
    for raw in (dict(base), dict(base, limit='0'), dict(base, limit='-5'), dict(base, limit='x')):
        with pytest.raises(InputError) as failure:
            Window.parse(raw)
        assert failure.value.code == 'BAD_WINDOW'
    assert Window.parse(dict(base, limit='50.0')).limit == Decimal('50.0')


def test_status_fault_minute_counts_as_gap_not_chain_break():
    # 仪器异常的缺证分钟与缺测一样记缺口，两侧高值仍属同一待查段。
    minutes = [high(i) for i in range(8)] + [row(8, state='missing', code='STATUS_FAULT')]
    minutes += [high(i) for i in range(9, 20)]
    event, = scan_events(minutes, window())
    assert event['status'] == 'PENDING'
    assert (event['gap_minutes'], event['gaps'][0]['code']) == (1, 'STATUS_FAULT')
