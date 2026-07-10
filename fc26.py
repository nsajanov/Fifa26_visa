# -*- coding: utf-8 -*-
"""FC26 (PS5) office cup: Swiss system + semifinals + final.
State lives in one JSON cell (state key 'fc26') — no schema changes needed.

Rules encoded here:
  * Swiss, FC_SWISS_ROUNDS rounds (default 3). Win 3 / draw 1 / loss 0.
  * Odd player count -> one BYE per round (3 pts, 0:0), lowest-ranked without a bye.
  * No rematches in Swiss.
  * Tie-breakers: points -> Buchholz (sum of opponents' points) -> goal diff -> goals for.
  * Top-4 -> semifinals (1v4, 2v3); winners -> FINAL, losers -> bronze.
  * Playoff draws are decided on penalties: report '2:2 <имя победителя>'.
"""
import os, json, random

SWISS_ROUNDS = int(os.getenv('FC_SWISS_ROUNDS', '3'))

def new_state():
    return {'players': [], 'rounds': [], 'stage': 'reg', 'po': {}}

# ---------- standings ----------
def _blank(name):
    return {'n': name, 'pts': 0, 'gf': 0, 'ga': 0, 'opps': [], 'bye': False, 'w': 0, 'd': 0, 'l': 0}

def standings(st):
    tab = {p['n']: _blank(p['n']) for p in st['players']}
    for rnd in st['rounds']:
        for i, (a, b) in enumerate(rnd['pairs']):
            res = rnd['res'].get(str(i))
            if not res:
                continue
            ga, gb = res
            A, B = tab[a], tab[b]
            A['gf'] += ga; A['ga'] += gb; B['gf'] += gb; B['ga'] += ga
            A['opps'].append(b); B['opps'].append(a)
            if ga > gb: A['pts'] += 3; A['w'] += 1; B['l'] += 1
            elif gb > ga: B['pts'] += 3; B['w'] += 1; A['l'] += 1
            else: A['pts'] += 1; B['pts'] += 1; A['d'] += 1; B['d'] += 1
        if rnd.get('bye') and rnd['bye'] in tab:
            tab[rnd['bye']]['pts'] += 3; tab[rnd['bye']]['w'] += 1; tab[rnd['bye']]['bye'] = True
    for t in tab.values():
        t['buch'] = sum(tab[o]['pts'] for o in t['opps'] if o in tab)
        t['gd'] = t['gf'] - t['ga']
    return sorted(tab.values(), key=lambda t: (-t['pts'], -t['buch'], -t['gd'], -t['gf'], t['n']))

def _played_pairs(st):
    out = set()
    for rnd in st['rounds']:
        for a, b in rnd['pairs']:
            out.add(frozenset((a, b)))
    return out

def _had_bye(st):
    return {r['bye'] for r in st['rounds'] if r.get('bye')}

# ---------- pairing ----------
def pair_round(st):
    """Greedy Swiss pairing by current ranking; returns {'pairs': [...], 'bye': name|None, 'res': {}}."""
    order = [t['n'] for t in standings(st)]
    if not st['rounds']:
        order = [p['n'] for p in st['players']]
        random.shuffle(order)
    played = _played_pairs(st)
    bye = None
    if len(order) % 2 == 1:
        byed = _had_bye(st)
        for n in reversed(order):                    # lowest-ranked without a bye
            if n not in byed:
                bye = n; break
        if bye is None:
            bye = order[-1]
        order = [n for n in order if n != bye]
    pairs, pool = [], order[:]
    def backtrack(pool):
        if not pool:
            return []
        a = pool[0]
        for j in range(1, len(pool)):
            b = pool[j]
            if frozenset((a, b)) in played:
                continue
            rest = backtrack([x for k, x in enumerate(pool) if k not in (0, j)])
            if rest is not None:
                return [(a, b)] + rest
        return None
    pairs = backtrack(pool)
    if pairs is None:                                # rematch unavoidable — allow it
        pairs = [(pool[i], pool[i + 1]) for i in range(0, len(pool), 2)]
    return {'pairs': [list(p) for p in pairs], 'bye': bye, 'res': {}}

def round_done(rnd):
    return all(str(i) in rnd['res'] for i in range(len(rnd['pairs'])))

# ---------- playoff ----------
def start_semis(st):
    top = [t['n'] for t in standings(st)[:4]]
    st['stage'] = 'semis'
    st['po'] = {'semis': {'pairs': [[top[0], top[3]], [top[1], top[2]]], 'res': {}, 'pen': {}},
                'final': None, 'bronze': None}
    return top

def semis_done(st):
    s = st['po'].get('semis') or {}
    return s and all(_po_winner(s, i) for i in range(2))

def _po_winner(block, i):
    res = block['res'].get(str(i))
    if not res:
        return None
    ga, gb = res
    a, b = block['pairs'][i]
    if ga > gb: return a
    if gb > ga: return b
    return block['pen'].get(str(i))                  # draw -> penalties winner

def _po_loser(block, i):
    w = _po_winner(block, i)
    if not w: return None
    a, b = block['pairs'][i]
    return b if w == a else a

def start_final(st):
    s = st['po']['semis']
    st['po']['final'] = {'pairs': [[_po_winner(s, 0), _po_winner(s, 1)]], 'res': {}, 'pen': {}}
    st['po']['bronze'] = {'pairs': [[_po_loser(s, 0), _po_loser(s, 1)]], 'res': {}, 'pen': {}}
    st['stage'] = 'final'

def final_done(st):
    f = st['po'].get('final') or {}
    return f and _po_winner(f, 0)

def champion(st):
    return _po_winner(st['po']['final'], 0) if final_done(st) else None

def bronze_winner(st):
    b = st['po'].get('bronze')
    return _po_winner(b, 0) if b else None

# ---------- current matches (for reporting scores) ----------
def current_block(st):
    """Returns (label, block) where block has pairs/res(/pen)."""
    if st['stage'] == 'swiss' and st['rounds']:
        return f"Тур {len(st['rounds'])}", st['rounds'][-1]
    if st['stage'] == 'semis':
        return 'Полуфиналы', st['po']['semis']
    if st['stage'] == 'final':
        # merge final+bronze into one block view: match 1 = final, 2 = bronze
        f, b = st['po']['final'], st['po']['bronze']
        return 'Финал', {'pairs': [f['pairs'][0], b['pairs'][0]],
                         'res': {'0': f['res'].get('0'), '1': b['res'].get('0')},
                         '_split': (f, b)}
    return None, None

def report(st, match_no, ga, gb, pen_winner=None):
    """match_no is 1-based within the current block. Returns (ok, message)."""
    label, block = current_block(st)
    if not block:
        return False, 'Сейчас нет активного тура.'
    i = match_no - 1
    if not (0 <= i < len(block['pairs'])):
        return False, f'Нет матча №{match_no} в блоке «{label}».'
    a, b = block['pairs'][i]
    if st['stage'] in ('semis', 'final') and ga == gb and not pen_winner:
        return False, 'В плей-офф ничьи нет: добавь победителя по пенальти, напр.  /fc 1 2:2 ' + a
    if pen_winner and pen_winner not in (a, b):
        return False, f'Победитель по пенальти должен быть {a} или {b}.'
    if st['stage'] == 'final':
        f, br = block['_split']
        target = f if i == 0 else br
        target['res']['0'] = [ga, gb]
        if pen_winner: target['pen']['0'] = pen_winner
    else:
        block['res'][str(i)] = [ga, gb]
        if pen_winner: block['pen'][str(i)] = pen_winner
    who = a if ga > gb else b if gb > ga else (pen_winner or 'ничья')
    return True, f'{label} · {a} {ga}:{gb} {b} → {who}'

# ---------- formatting ----------
def fmt_round(st):
    label, block = current_block(st)
    if not block:
        return 'Турнир ещё не начат (или уже завершён).'
    lines = [f'🎮 <b>FC26 · {label}</b>']
    for i, (a, b) in enumerate(block['pairs'], 1):
        res = block['res'].get(str(i - 1))
        tag = '🏆 Финал' if (st['stage'] == 'final' and i == 1) else ('🥉 Бронза' if st['stage'] == 'final' else f'М{i}')
        if res:
            lines.append(f'{tag}: {a} <b>{res[0]}:{res[1]}</b> {b}')
        else:
            lines.append(f'{tag}: {a} — {b}   <i>(счёт: /fc {i} X:Y)</i>')
    if st['stage'] == 'swiss' and st['rounds'] and st['rounds'][-1].get('bye'):
        lines.append(f"😴 Отдыхает: {st['rounds'][-1]['bye']} (+3 очка)")
    return '\n'.join(lines)

def fmt_table(st):
    rows = standings(st)
    if not rows:
        return 'Пока никого нет. Регистрация: /fc join'
    medals = {1: '🥇', 2: '🥈', 3: '🥉'}
    lines = ['🎮 <b>FC26 · Таблица (швейцарка)</b>', '<i>очки · Бухгольц · разница</i>']
    for i, t in enumerate(rows, 1):
        lines.append(f"{medals.get(i, str(i)+'.')} {t['n']} — <b>{t['pts']}</b> · {t['buch']} · {t['gd']:+d}")
    if st['stage'] in ('semis', 'final'):
        lines.append('')
        s = st['po']['semis']
        lines.append('Полуфиналы: ' + ' | '.join(f"{p[0]}–{p[1]}" for p in s['pairs']))
    if st['stage'] == 'final' and final_done(st):
        lines.append(f'🏆 <b>ЧЕМПИОН: {champion(st)}</b>')
        if bronze_winner(st):
            lines.append(f'🥉 Бронза: {bronze_winner(st)}')
    return '\n'.join(lines)
