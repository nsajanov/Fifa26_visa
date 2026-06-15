# -*- coding: utf-8 -*-
"""Playoff-only scoring. Group stage = facts (not scored). The game starts at the
knockout: everyone predicts the same real R32 bracket, then earns points.

Per knockout match the player predicts the score and the winner.
 - correct winner: round weight (R32 3 / R16 5 / QF 8 / SF 12 / Bronze 5 / Final 25)
 - score bonus (only if the player predicted the same two teams in that match):
   5 exact / 3 goal-difference / 1 outcome
Champion = winner of the Final (captured by the high Final weight)."""
from fixtures import ROUND_OF, EXACT, GOALDIFF, OUTCOME

KO_WIN_POINTS = {'R32': 3, 'R16': 5, 'QF': 8, 'SF': 12, 'Bronze': 5, 'Final': 25}

def _num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None

def _result_points(pred, actual):
    ph, pa = _num(pred[0]), _num(pred[1])
    ah, aa = _num(actual[0]), _num(actual[1])
    if None in (ph, pa, ah, aa):
        return 0
    if ph == ah and pa == aa:
        return EXACT
    if ph - pa == ah - aa:
        return GOALDIFF
    if (ph - pa > 0) == (ah - aa > 0) and (ph - pa == 0) == (ah - aa == 0):
        return OUTCOME
    return 0

def score_submission(pred, actual):
    """pred/actual = {'ko': {match_str: {home,away,w,hs,as}}}."""
    pko = pred.get('ko', {}); ako = actual.get('ko', {})
    win_pts = 0; bonus = 0; exact = 0; correct = 0
    for m, rnd in ROUND_OF.items():
        sm = str(m)
        p = pko.get(sm); a = ako.get(sm)
        if not p or not a:
            continue
        aw = a.get('w')
        if aw and p.get('w') == aw:
            win_pts += KO_WIN_POINTS[rnd]; correct += 1
        # score bonus only if same matchup predicted
        if {p.get('home'), p.get('away')} == {a.get('home'), a.get('away')} and None not in (p.get('home'), p.get('away')) and a.get('home'):
            pred_for = {p.get('home'): p.get('hs'), p.get('away'): p.get('as')}
            pts = _result_points((pred_for.get(a['home']), pred_for.get(a['away'])),
                                  (a.get('hs'), a.get('as')))
            bonus += pts
            exact += (pts == EXACT)
    return {'total': win_pts + bonus, 'win_pts': win_pts, 'bonus': bonus,
            'correct': correct, 'exact': exact,
            'champion_ok': bool(pko.get('104', {}).get('w') and
                                pko.get('104', {}).get('w') == ako.get('104', {}).get('w'))}

def leaderboard(submissions, actual):
    rows = [(name, score_submission(sub, actual)) for name, sub in submissions.items()]
    rows.sort(key=lambda r: (-r[1]['total'], -r[1]['exact'], -r[1]['win_pts'], r[0]))
    return rows
