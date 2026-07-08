# -*- coding: utf-8 -*-
"""football-data.org adapter + resolver.
v2: carries score.winner / duration / status through (penalty shootouts!),
and normalizes team names to the BRACKET name space used by player picks."""
import os, json, urllib.request
from fixtures import GROUPS, KO, ROUND_OF

API = 'https://api.football-data.org/v4/competitions/WC/matches'
STAGE_CODES = {'R32': {'LAST_32'}, 'R16': {'LAST_16'}, 'QF': {'QUARTER_FINALS', 'QUARTER_FINAL'},
               'SF': {'SEMI_FINALS', 'SEMI_FINAL'}, 'Bronze': {'THIRD_PLACE'}, 'Final': {'FINAL'}}

# football-data names -> our bracket names (the space player picks are stored in)
ALIAS = {'Korea Republic': 'South Korea', 'Türkiye': 'Turkiye', 'Turkey': 'Turkiye',
         'USA': 'United States', 'Czech Republic': 'Czechia',
         'DR Congo': 'DR Congo', 'Congo DR': 'DR Congo',
         "Côte d'Ivoire": "Cote d'Ivoire", 'Ivory Coast': "Cote d'Ivoire",
         'Cote d Ivoire': "Cote d'Ivoire", 'Curaçao': 'Curacao',
         'Bosnia-Herzegovina': 'Bosnia and Herzegovina',
         'Cape Verde': 'Cabo Verde', 'Cape Verde Islands': 'Cabo Verde',
         'Iran': 'IR Iran', 'United States of America': 'United States'}

def norm(name):
    return ALIAS.get(name, name)

# ---------- HTTP (only when a token is set) ----------
def fetch_sync(token):
    """Fetch ALL matches. Each item now includes status/winner/duration so that
    knockout games decided in extra time or on penalties resolve correctly."""
    req = urllib.request.Request(API, headers={'X-Auth-Token': token})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    out = []
    for m in data.get('matches', []):
        sc = m.get('score') or {}
        ft = sc.get('fullTime') or {}
        pen = sc.get('penalties') or {}
        out.append({'stage': m.get('stage'), 'group': (m.get('group') or '').replace('GROUP_', ''),
                    'home': norm((m.get('homeTeam') or {}).get('name', '')),
                    'away': norm((m.get('awayTeam') or {}).get('name', '')),
                    'hs': ft.get('home'), 'as': ft.get('away'),
                    'winner': sc.get('winner'),            # HOME_TEAM / AWAY_TEAM / DRAW / None
                    'duration': sc.get('duration'),        # REGULAR / EXTRA_TIME / PENALTY_SHOOTOUT
                    'ph': pen.get('home'), 'pa': pen.get('away'),
                    'status': m.get('status'),             # FINISHED / TIMED / IN_PLAY ...
                    'utcDate': m.get('utcDate')})
    return out

# ---------- pure resolvers (testable offline) ----------
def match_winner(m):
    """Winner of a (possibly ET/penalties) match. None if not decided."""
    if m.get('winner') == 'HOME_TEAM':
        return m.get('home')
    if m.get('winner') == 'AWAY_TEAM':
        return m.get('away')
    hs, as_ = m.get('hs'), m.get('as')
    if hs is None or as_ is None:
        return None
    if hs > as_:
        return m.get('home')
    if as_ > hs:
        return m.get('away')
    ph, pa = m.get('ph'), m.get('pa')
    if ph is not None and pa is not None and ph != pa:
        return m.get('home') if ph > pa else m.get('away')
    return None

def standings(group_matches):
    table = {g: {t: {'pts': 0, 'gf': 0, 'ga': 0} for t in GROUPS[g]} for g in GROUPS}
    for m in group_matches:
        g = m.get('group'); h, a = m.get('home'), m.get('away')
        hs, as_ = m.get('hs'), m.get('as')
        if g not in table or h not in table[g] or a not in table[g] or hs is None or as_ is None:
            continue
        table[g][h]['gf'] += hs; table[g][h]['ga'] += as_
        table[g][a]['gf'] += as_; table[g][a]['ga'] += hs
        if hs > as_: table[g][h]['pts'] += 3
        elif as_ > hs: table[g][a]['pts'] += 3
        else: table[g][h]['pts'] += 1; table[g][a]['pts'] += 1
    ordered = {}
    for g, tt in table.items():
        rows = sorted(tt.items(), key=lambda kv: (-kv[1]['pts'], -(kv[1]['gf'] - kv[1]['ga']), -kv[1]['gf'], kv[0]))
        ordered[g] = [t for t, _ in rows]
    return ordered

def tables(group_matches):
    tb = {g: {t: {'p': 0, 'w': 0, 'd': 0, 'l': 0, 'gf': 0, 'ga': 0} for t in GROUPS[g]} for g in GROUPS}
    for m in group_matches:
        g, h, a = m.get('group'), m.get('home'), m.get('away')
        hs, as_ = m.get('hs'), m.get('as')
        if g not in tb or h not in tb[g] or a not in tb[g] or hs is None or as_ is None:
            continue
        tb[g][h]['p'] += 1; tb[g][a]['p'] += 1
        tb[g][h]['gf'] += hs; tb[g][h]['ga'] += as_
        tb[g][a]['gf'] += as_; tb[g][a]['ga'] += hs
        if hs > as_: tb[g][h]['w'] += 1; tb[g][a]['l'] += 1
        elif as_ > hs: tb[g][a]['w'] += 1; tb[g][h]['l'] += 1
        else: tb[g][h]['d'] += 1; tb[g][a]['d'] += 1
    out = {}
    for g, tt in tb.items():
        rows = []
        for t, d in tt.items():
            rows.append({'team': t, 'p': d['p'], 'w': d['w'], 'd': d['d'], 'l': d['l'],
                         'gf': d['gf'], 'ga': d['ga'], 'gd': d['gf'] - d['ga'],
                         'pts': d['w'] * 3 + d['d']})
        rows.sort(key=lambda r: (-r['pts'], -r['gd'], -r['gf'], r['team']))
        out[g] = rows
    return out

def build_actual(all_matches):
    group_m = [m for m in all_matches if (m.get('stage') == 'GROUP_STAGE' or m.get('group'))]
    st = standings(group_m)
    groups_done = all(len([1 for m in group_m if m.get('group') == g]) >= 6 for g in GROUPS)
    results = [{'group': m.get('group'), 'home': m['home'], 'away': m['away'],
                'hs': m['hs'], 'as': m['as'], 'date': m.get('utcDate')} for m in group_m
               if m.get('hs') is not None and m.get('as') is not None]
    return {'standings': st, 'tables': tables(group_m), 'results': results,
            'groups_done': groups_done, 'raw': all_matches}
