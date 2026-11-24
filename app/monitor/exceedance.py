"""连续高值分钟扫描与超限事件识别。

规则（与 docs/DATA-DICTIONARY.md 一致）：

- 有效分钟的参比浓度严格高于限值记为高值；连续高值按分钟序列成段，扫描跨越整点，
  因此跨整点的高值分钟可以接续为同一段。
- 工况免计分钟（启停/停机）不属于有效浓度序列，直接断开高值段。
- 缺证分钟（缺测、仪器异常、同版冲突等）记为缺口：缺口两侧的高值分钟仍归入同一待查
  段，但不得直接判超限，避免「缺一分钟即误报」。
- 一段内带缺口时先以 PENDING 交接值班员排查（缺口尚未补齐，无论当前已累计多少高值
  分钟都不得直接判超限），晚到记录补齐后复算转为确证或撤销；
- 无缺口且高值分钟达到 CONFIRM_MINUTES（15）分钟，事件确证超限；
  无缺口且不足 15 分钟的段不构成处置事件。
"""

CONFIRM_MINUTES = 15


def scan_events(minutes, window):
    """按整窗分钟序列扫描，返回超限/待研判事件列表。

    minutes 为 validity 阶段的逐分钟证据行（顺序与窗口分钟对齐），每行含
    state（valid/exempt/missing）、value（有效分钟的参比浓度 Decimal）与 code。
    """
    segments = []
    current = None
    for row in minutes:
        state, value = row['state'], row['value']
        if state == 'exempt':
            # 停机/启停免计：高值段硬断开，缺口不得跨过免计分钟接续。
            if current is not None:
                segments.append(current)
                current = None
            continue
        is_high = state == 'valid' and value is not None and value > window.limit
        if is_high:
            if current is None:
                current = {'start': row['utc'], 'end': row['utc'], 'high': 0, 'gaps': []}
            current['end'] = row['utc']
            current['high'] += 1
        elif state == 'missing':
            if current is not None:
                # 缺口先挂在当前段上；若后续再无高值，收尾时按不足成段处理。
                current['gaps'].append({'utc': row['utc'], 'local': row['local'], 'code': row['code']})
        else:
            # 有效但未超限的分钟同样断开（浓度已回落到限值以内）。
            if current is not None:
                segments.append(current)
                current = None
    if current is not None:
        segments.append(current)

    events = []
    for segment in segments:
        if not segment['gaps'] and segment['high'] < CONFIRM_MINUTES:
            # 无缺口的短高值段既非超限也无缺测待补，不产生处置事件。
            continue
        if segment['gaps']:
            # 带缺口的段无论当前累计多少高值分钟都先挂 PENDING：缺测未补齐时
            # 直接确证会误报，等晚到记录复算后同一事件再转 CONFIRMED/REVOKED。
            status = 'PENDING'
        else:
            status = 'CONFIRMED'
        events.append({
            'event_id': event_key(window, segment['start']),
            'status': status,
            'start': segment['start'],
            'end': segment['end'],
            'high_minutes': segment['high'],
            'gap_minutes': len(segment['gaps']),
            'gaps': segment['gaps'],
            'confirm_minutes': CONFIRM_MINUTES,
            'limit_mg_nm3': str(window.limit),
        })
    return events


def event_key(window, start_utc):
    """事件业务键：烟道 + 窗口起点 + 首个高值分钟，重跑间保持稳定。"""
    return f'{window.stack}|{window.start.isoformat()}|{start_utc}'
