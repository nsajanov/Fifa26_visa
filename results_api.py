# -*- coding: utf-8 -*-
"""football-data.org adapter + resolver.
Pulls finished WC matches, derives group standings (facts) and the real knockout
bracket — without needing FIFA's 495-combination table. Each Round-of-32 slot is
anchored by its known group position (e.g. 1E, 2A); the opposing 3rd-placed team is
simply read from whatever match that anchor team actually plays."""
import os, json, urllib.request
from fixtures import GROUPS, KO, ROUND_OF

API = 'https://api.football-data.org/v4/competitions/WC/matches'
STAGE_CODES = {'R32': {'LAST_32'}, 'R16': {'LAST_16'}, 'QF': {'QUARTER_FINALS', 'QUARTER_FINAL'},
               'SF': {'SEMI_FINALS', 'SEMI_FINAL'}, 'Bronze': {'THIRD_PLACE'}, 'Final': {'FINAL'}}

# football-data names -> our names (extend if the API uses different spellings)
ALIAS = {'Korea Republic': 'South Korea', 'IR Iran': 'IR Iran', 'Turkey': 'Turkiye',
         'Türkiye': 'Turkiye', 'USA': 'United States', 'Cabo Verde': 'Cabo Verde',
         'Czech Republic': 'Czechia', 'DR Congo': 'Congo DR', "Côte d'Ivoire": 'Cote d Ivoire',
         'Ivory Coast': 'Cote d Ivoire', 'Curaçao': 'Curacao'}

def norm(name):
    return ALIAS.get(name, name)

# ---------- HTTP (only when a token is set) ----------
def fetch_sync(token):
    req = urllib.request.Request(API + '?status=FINISHED', headers={'X-Auth-Token': token})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    out = []
    for m in data.get('matches', []):
        ft = (m.get('score') or {}).get('fullTime') or {}
        out.append({'stage': m.get('stage'), 'group': (m.get('group') or '').replace('GROUP_', ''),
                    'home': norm((m.get('homeTeam') or {}).get('name', '')),
                    'away': norm((m.get('awayTeam') or {}).get('name', '')),
                    'hs': ft.get('home'), 'as': ft.get('away'), 'utcDate': m.get('utcDate')})
    return out

# ---------- pure resolvers (testable offline) ----------
def standings(group_matches):
    """group_matches: list of {group,home,away,hs,as}. Returns {group: [team,...] ordered}."""
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

def _slot_team(slot, st):
    pos = int(slot[0]); g = slot[1]
    teams = st.get(g, [])
    return teams[pos - 1] if len(teams) >= pos else None

def _find(ko_pool, round_name, must_have, exclude=()):
    """find a knockout match in the given round containing all `must_have` teams."""
    for mm in ko_pool:
        if mm['stage'] not in STAGE_CODES[round_name]:
            continue
        pair = {mm['home'], mm['away']}
        if all(t in pair for t in must_have if t) and not (pair & set(exclude) and False):
            return mm
    return None

def _winner(mm):
    if mm['hs'] is None or mm['as'] is None:
        return None
    if mm['hs'] > mm['as']:
        return mm['home']
    if mm['as'] > mm['hs']:
        return mm['away']
    return None  # draw in regulation: API "winner" field would be needed; left for manual /win

def build_actual(all_matches):
    """Returns {'ko': {match_str: {home,away,w,hs,as}}, 'standings': {...}, 'groups_done': bool}."""
    group_m = [m for m in all_matches if (m.get('stage') == 'GROUP_STAGE' or m.get('group'))]
    ko_pool = [m for m in all_matches if m.get('stage') in
               {c for s in STAGE_CODES.values() for c in s}]
    st = standings(group_m)
    groups_done = all(len([1 for m in group_m if m.get('group') == g]) >= 6 for g in GROUPS)
    ko = {}
    def teams_of(m):
        return ko.get(str(m), {}).get('home'), ko.get(str(m), {}).get('away')
    for m in range(73, 105):
        rnd = ROUND_OF[m]
        s0, s1 = KO[m]
        if rnd == 'R32':
            known = [t for t in (_slot_team(s0, st) if isinstance(s0, str) and not s0.startswith('3-') else None,
                                  _slot_team(s1, st) if isinstance(s1, str) and not s1.startswith('3-') else None) if t]
            mm = _find(ko_pool, 'R32', known) if known else None
            if not mm:
                continue
            ko[str(m)] = {'home': mm['home'], 'away': mm['away'], 'w': _winner(mm),
                          'hs': mm['hs'], 'as': mm['as']}
        else:
            # feeders: ('W'/'L', match)
            def feed(slot):
                kind, fm = slot
                a = ko.get(str(fm))
                if not a or not a.get('w'):
                    return None
                if kind == 'W':
                    return a['w']
                return a['away'] if a['w'] == a['home'] else a['home']
            t0, t1 = feed(s0), feed(s1)
            if not t0 or not t1:
                continue
            mm = _find(ko_pool, rnd, [t0, t1])
            if not mm:
                continue
            ko[str(m)] = {'home': mm['home'], 'away': mm['away'], 'w': _winner(mm),
                          'hs': mm['hs'], 'as': mm['as']}
    return {'ko': ko, 'standings': st, 'groups_done': groups_done, 'raw': all_matches}
