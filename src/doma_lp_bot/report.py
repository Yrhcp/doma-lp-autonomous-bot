"""Conservative analytics over recorded observations, never an investment P&L."""
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from .monitor import position_identity, scoped
from .storage import log_files, FILE_COUNT

D=Decimal

def parse_time(value):
    x=datetime.fromisoformat(value)
    if x.tzinfo is None: raise ValueError('timezone required')
    return x

def dec(v):
    x=D(str(v))
    if not x.is_finite(): raise ValueError('nonfinite')
    return x

def utilization_stats(values):
    if not values: return None
    return {'observations':len(values),'first':values[0],'last':values[-1],
            'min':min(values),'max':max(values),'mean':sum(values)/len(values)}

def analyze(path, cfg, latest_watch=False):
    records=[]; warnings=[]; malformed=0
    files=log_files(path)
    if not files: warnings.append('Журнал не найден.')
    if Path(path).with_name(Path(path).name+f'.{FILE_COUNT-1}').exists():
        warnings.append('Ротация достигла предела: более старая история могла быть удалена; отчёт охватывает только сохранённые файлы.')
    for file in files:
        with file.open(encoding='utf8',errors='replace') as f:
            for line in f:
                try:
                    row=json.loads(line)
                    if not isinstance(row,dict): raise ValueError('not an object')
                    if row.get('event')=='log_recovery': records.append({'event':'corrupt_record'}); malformed+=1
                    elif row.get('event') in ('error','gap','reconnect','simulation_error') and 'chain' not in row:
                        warnings.append('Есть старые ошибки без идентификатора кошелька/сети; их число не включено.')
                    elif scoped(row,cfg): records.append(row)
                except (ValueError,TypeError,AttributeError):
                    malformed+=1;records.append({'event':'corrupt_record'})
    warnings.append('Отчёт охватывает только сохранённые ротированные файлы; удалённая история недоступна.')
    report_scope='all_history'
    if latest_watch:
        starts=[i for i,row in enumerate(records) if row.get('event')=='watch_start']
        if starts:
            start=starts[-1]; wid=records[start].get('watch_id')
            stop=next((i+1 for i in range(start+1,len(records)) if records[i].get('event')=='watch_complete' and records[i].get('watch_id')==wid),len(records))
            records=records[start:stop]; report_scope='latest_watch'
        else:
            warnings.append('Окно наблюдения не найдено: показан весь доступный журнал.')
    if malformed: warnings.append(f'Повреждённых строк: {malformed}; отсутствующие данные не восстановлены.')
    event_times=[]
    for row in records:
        try: event_times.append(parse_time(row['time']))
        except (KeyError,ValueError,TypeError):
            if row.get('event') != 'corrupt_record': warnings.append('Запись без корректного времени не учитывается в границах периода.')
    if any(event_times[i] < event_times[i-1] for i in range(1,len(event_times))): warnings.append('Время событий идёт назад; границы периода могут быть ненадёжны.')
    snapshots=[]; errors=0; rpc_errors=0; unclassified_errors=0; partial=0; gaps=0; inferred=0; signals=0; reconnections=0
    pending_break=False; last=None; periods=[]; active=None
    samples_a=[]; samples_b=[]; all_contiguous=True
    def close_period():
        nonlocal active
        if active is not None: periods.append(active); active=None
    for row in records:
        ev=row.get('event')
        if ev=='error':
            errors+=1
            if row.get('error_category')=='rpc': rpc_errors+=1
            elif 'error_category' not in row: unclassified_errors+=1
            pending_break=True; all_contiguous=False; close_period(); continue
        if ev=='partial_data': partial+=1; continue
        if ev=='corrupt_record': pending_break=True; all_contiguous=False; close_period(); continue
        if ev=='gap': gaps+=1; pending_break=True; all_contiguous=False; close_period(); continue
        if ev in ('watch_paused','watch_complete'):
            pending_break=True; close_period(); continue
        if ev=='reconnect': reconnections+=1; continue
        if ev=='signal': signals+=1; continue
        if ev!='snapshot': continue
        try:
            t=parse_time(row['time']); price=dec(row['price_usdc_per_weth']); weth=dec(row['weth']); usdc=dec(row['usdc'])
            if price <= 0 or weth < 0 or usdc < 0 or row['status'] not in ('IN_RANGE','OUT_OF_RANGE'): raise ValueError('invalid snapshot')
            ident=position_identity(row)
            block=int(row['block'])
            side=row.get('buffered_exit')
            if row['status']=='OUT_OF_RANGE' and row.get('lower_price') is not None and row.get('upper_price') is not None:
                if price <= dec(row['lower_price']): side='below'
                elif price >= dec(row['upper_price']): side='above'
        except (ValueError,TypeError,KeyError,InvalidOperation):
            warnings.append('Снимок с повреждёнными или отсутствующими данными исключён.'); pending_break=True; all_contiguous=False; close_period(); continue
        elapsed=None if last is None else (t-last['t']).total_seconds()
        broken=last is not None and (pending_break or elapsed < 0 or elapsed > cfg.max_observation_gap or block <= last['block'] or ident != last['ident'])
        if broken:
            if not pending_break: inferred+=1
            all_contiguous=False; close_period()
        if row['status']=='OUT_OF_RANGE':
            if active is not None and side and active.get('side') and side != active['side']:
                warnings.append('Соседние снимки оказались по разные стороны диапазона; промежуточный переход неизвестен.')
                close_period()
            if active is None: active={'first':t.isoformat(),'last':t.isoformat(),'sample_count':1,'covered_seconds':0.0,'token_id':row['token_id'],'side':side}
            else:
                active['last']=t.isoformat(); active['sample_count']+=1
                if last is not None and not broken and last['status']=='OUT_OF_RANGE': active['covered_seconds']+=elapsed
        else: close_period()
        sim=row.get('simulation')
        if isinstance(sim,dict):
            try:
                value_a=dec(sim['strategy_a']['capital_utilization_before_costs_percent'])
                value_b=dec(sim['strategy_b']['capital_utilization_before_costs_percent'])
                samples_a.append(value_a); samples_b.append(value_b)
            except (ValueError,KeyError,TypeError,InvalidOperation):
                warnings.append('Отдельная симуляция не содержит сравнимых значений A/B.')
        item={'row':row,'t':t,'price':price,'value':usdc+weth*price,'ident':ident,'block':block,'status':row['status']}
        snapshots.append(item); last=item; pending_break=False
    close_period()
    n=len(snapshots)
    if partial: warnings.append(f'Неполных снимков (например fees): {partial}.')
    if unclassified_errors: warnings.append(f'Ошибок без категории: {unclassified_errors}; не включены в число ошибок RPC.')
    if n and event_times and (snapshots[0]['t'] > event_times[0] or snapshots[-1]['t'] < event_times[-1]):
        warnings.append('Начало/конец журнала не покрыты снимками; состояние на краях неизвестно.')
    if n<2: warnings.append('Менее двух снимков: динамика и продолжительность не определены.')
    if gaps or inferred or errors or malformed: warnings.append('Есть пропуски или ошибки: состояние между снимками неизвестно.')
    warnings.append('Оценка периодов вне диапазона основана на соседних снимках; промежуточные входы/выходы неизвестны.')
    warnings.append('Изменение накопленных комиссий не равно чистой прибыли; стоимость LP не равна P&L без остатков кошелька, вложений, переводов, комиссий, цен и непостоянных потерь.')
    result={'scope':report_scope,'start':event_times[0].isoformat() if event_times else None,'end':event_times[-1].isoformat() if event_times else None,
            'first_snapshot':snapshots[0]['t'].isoformat() if n else None,'last_snapshot':snapshots[-1]['t'].isoformat() if n else None,
            'elapsed_seconds':(event_times[-1]-event_times[0]).total_seconds() if len(event_times)>1 and event_times[-1]>=event_times[0] else None,'snapshots':n,
            'price_min':min((x['price'] for x in snapshots),default=None),'price_max':max((x['price'] for x in snapshots),default=None),
            'price_sample_mean':sum((x['price'] for x in snapshots),D(0))/n if n else None,
            'in_range_count':sum(x['status']=='IN_RANGE' for x in snapshots),'out_of_range_count':sum(x['status']=='OUT_OF_RANGE' for x in snapshots),
            'in_range_percent':D(sum(x['status']=='IN_RANGE' for x in snapshots))*100/n if n else None,
            'out_of_range_percent':D(sum(x['status']=='OUT_OF_RANGE' for x in snapshots))*100/n if n else None,
            'confirmed_signals':signals,'out_of_range_periods':periods,'errors_total':errors,'rpc_errors':rpc_errors,'unclassified_errors':unclassified_errors,'partial_data_events':partial,'recorded_gaps':gaps,'inferred_gaps':inferred,
            'reconnections':reconnections,'principal_first_usdc':snapshots[0]['value'] if n else None,
            'principal_last_usdc':snapshots[-1]['value'] if n else None,'principal_change_usdc':None,
            'fees_change_estimate':None,'strategy_a_utilization_before_costs':utilization_stats(samples_a),
            'strategy_b_utilization_before_costs':utilization_stats(samples_b),'warnings':warnings}
    if n>=2:
        first=snapshots[0]; end=snapshots[-1]
        stable_id=all(x['ident']==first['ident'] for x in snapshots)
        if stable_id:
            result['principal_change_usdc']=end['value']-first['value']
        else: warnings.append('NFT, диапазон или ликвидность изменились: изменение стоимости не сопоставимо.')
        try:
            # No accounting across gaps, collects, position updates, changed owed or missing legacy baseline.
            keys=('fee_growth_last0_x128','fee_growth_last1_x128','tokens_owed0_raw','tokens_owed1_raw')
            baseline=tuple(first['row'][k] for k in keys)
            if not stable_id or not all_contiguous or any(tuple(x['row'][k] for k in keys)!=baseline for x in snapshots): raise ValueError('changed state')
            fees=[x['row']['fees'] for x in snapshots]
            if any(f['status']!='estimated_from_fee_growth' for f in fees): raise ValueError('fee data missing')
            w=[dec(f['accrued_since_update_weth']) for f in fees]; u=[dec(f['accrued_since_update_usdc']) for f in fees]
            if any(w[i]<w[i-1] or u[i]<u[i-1] for i in range(1,len(w))): raise ValueError('fee reset')
            result['fees_change_estimate']={'weth':w[-1]-w[0],'usdc':u[-1]-u[0]}
            warnings.append('Оценка изменения комиссий предполагает отсутствие незамеченного collect/изменения позиции между опросами; это не подтверждено событиями.')
        except (ValueError,KeyError,TypeError,InvalidOperation):
            warnings.append('Изменение комиссий UNKNOWN: пропуск, изменение состояния, сброс или отсутствуют базовые данные.')
    if n and (len(samples_a)!=n or len(samples_b)!=n): warnings.append('A/B представлены не для каждого снимка; старые записи могли не содержать симуляции.')
    return result

def render(result):
    def val(x,precision=4): return 'UNKNOWN' if x is None else f'{x:.{precision}f}'
    r=result
    lines=[f"Область отчёта: {r['scope']}", f"Период: {r['start'] or 'UNKNOWN'} — {r['end'] or 'UNKNOWN'}; длительность: {val(r['elapsed_seconds'],1)} с; снимков: {r['snapshots']}",
        f"Цена USDC.e/WETH min / max / средняя по снимкам: {val(r['price_min'],6)} / {val(r['price_max'],6)} / {val(r['price_sample_mean'],6)}",
        f"IN_RANGE: {r['in_range_count']} ({val(r['in_range_percent'],2)}%); OUT_OF_RANGE: {r['out_of_range_count']} ({val(r['out_of_range_percent'],2)}%); сигналов: {r['confirmed_signals']}",
        f"Ошибок всего / RPC: {r['errors_total']} / {r['rpc_errors']}; разрывов записанных/выявленных: {r['recorded_gaps']}/{r['inferred_gaps']}; восстановлений: {r['reconnections']}",
        f"Стоимость основной LP, первая / последняя / изменение USDC.e: {val(r['principal_first_usdc'],6)} / {val(r['principal_last_usdc'],6)} / {val(r['principal_change_usdc'],6)}"]
    f=r['fees_change_estimate']
    lines.append('Изменение оценки комиссий: UNKNOWN' if f is None else f"Изменение оценки комиссий: {val(f['weth'],12)} WETH; {val(f['usdc'],6)} USDC.e")
    for k in ('a','b'):
        u=r[f'strategy_{k}_utilization_before_costs']
        lines.append(f"Стратегия {k.upper()}, использование до затрат first / last / min / max / mean: UNKNOWN" if not u else f"Стратегия {k.upper()}, использование до затрат first / last / min / max / mean: {val(u['first'],2)} / {val(u['last'],2)} / {val(u['min'],2)} / {val(u['max'],2)} / {val(u['mean'],2)}% ({u['observations']} снимков)")
    lines.append('Периоды OUT_OF_RANGE:')
    if not r['out_of_range_periods']: lines.append('  Не наблюдались.' if r['snapshots'] else '  UNKNOWN')
    for x in r['out_of_range_periods']:
        lines.append(f"  {x['first']} — {x['last']}: {x['covered_seconds']:.1f} с между соседними снимками, снимков {x['sample_count']}, NFT {x['token_id']}")
    lines.append('Предупреждения:')
    lines.extend('  - '+x for x in dict.fromkeys(r['warnings']))
    return '\n'.join(lines)
