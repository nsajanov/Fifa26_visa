# -*- coding: utf-8 -*-
"""Tournament data: groups, group fixtures, knockout structure (WC 2026)."""

GROUPS = {
 'A': ['Mexico', 'South Africa', 'South Korea', 'Czechia'],
 'B': ['Canada', 'Bosnia and Herzegovina', 'Qatar', 'Switzerland'],
 'C': ['Brazil', 'Morocco', 'Haiti', 'Scotland'],
 'D': ['United States', 'Paraguay', 'Australia', 'Turkiye'],
 'E': ['Germany', 'Curacao', 'Cote d Ivoire', 'Ecuador'],
 'F': ['Netherlands', 'Japan', 'Sweden', 'Tunisia'],
 'G': ['Belgium', 'Egypt', 'IR Iran', 'New Zealand'],
 'H': ['Spain', 'Cabo Verde', 'Saudi Arabia', 'Uruguay'],
 'I': ['France', 'Senegal', 'Iraq', 'Norway'],
 'J': ['Argentina', 'Algeria', 'Austria', 'Jordan'],
 'K': ['Portugal', 'Congo DR', 'Uzbekistan', 'Colombia'],
 'L': ['England', 'Croatia', 'Ghana', 'Panama'],
}

# Round-robin within each group: every pair plays once -> 6 matches per group, 72 total.
# match id = group letter + index, e.g. "A1".
_RR = [(0, 1), (2, 3), (0, 2), (1, 3), (0, 3), (1, 2)]

def build_group_matches():
    matches = []
    for g, teams in GROUPS.items():
        for i, (h, a) in enumerate(_RR, start=1):
            matches.append({'id': f'{g}{i}', 'group': g,
                            'home': teams[h], 'away': teams[a]})
    return matches

GROUP_MATCHES = build_group_matches()  # 72 fixtures

# Knockout structure (official WC2026 bracket). slot codes: "1A"=winner A, "2B"=runner-up B,
# "3-ABCDF"=a 3rd-placed team from groups A/B/C/D/F. For later rounds: ("W", 73)=winner of M73.
KO = {
 73: ('2A', '2B'), 74: ('1E', '3-ABCDF'), 75: ('1F', '2C'), 76: ('1C', '2F'),
 77: ('1I', '3-CDFGH'), 78: ('2E', '2I'), 79: ('1A', '3-CEFHI'), 80: ('1L', '3-EHIJK'),
 81: ('1D', '3-BEFIJ'), 82: ('1G', '3-AEHIJ'), 83: ('2K', '2L'), 84: ('1H', '2J'),
 85: ('1B', '3-EFGIJ'), 86: ('1J', '2H'), 87: ('1K', '3-DEIJL'), 88: ('2D', '2G'),
 89: (('W', 74), ('W', 77)), 90: (('W', 73), ('W', 75)), 91: (('W', 76), ('W', 78)),
 92: (('W', 79), ('W', 80)), 93: (('W', 83), ('W', 84)), 94: (('W', 81), ('W', 82)),
 95: (('W', 86), ('W', 88)), 96: (('W', 85), ('W', 87)),
 97: (('W', 89), ('W', 90)), 98: (('W', 93), ('W', 94)), 99: (('W', 91), ('W', 92)),
 100: (('W', 95), ('W', 96)),
 101: (('W', 97), ('W', 98)), 102: (('W', 99), ('W', 100)),
 103: (('L', 101), ('L', 102)),   # bronze
 104: (('W', 101), ('W', 102)),   # final
}
ROUND_OF = {**{m: 'R32' for m in range(73, 89)}, **{m: 'R16' for m in range(89, 97)},
            **{m: 'QF' for m in range(97, 101)}, 101: 'SF', 102: 'SF',
            103: 'Bronze', 104: 'Final'}

# bracket points per correct team, by round (matches the Excel model)
ROUND_POINTS = {'R32': 1, 'R16': 2, 'QF': 3, 'SF': 5, 'Final': 7}
CHAMPION_POINTS = 15
# score-bonus per knockout/group match: exact / goal-diff / outcome
EXACT, GOALDIFF, OUTCOME = 5, 3, 1

ALL_TEAMS = sorted({t for ts in GROUPS.values() for t in ts})
