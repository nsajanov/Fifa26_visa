# -*- coding: utf-8 -*-
"""Single-elimination bracket of 32 teams for the in-chat predictor.
EDIT R32_PAIRS with the real Round-of-32 matchups once the 32 teams are known.
The whole bracket (R16 -> Final) is built automatically from these 16 pairs."""

# 16 Round-of-32 matchups (home, away), in bracket order (adjacent pairs meet next round).
# Real WC 2026 knockout bracket (confirmed after the group stage).
R32_PAIRS = [
    ("Germany", "Paraguay"),                      # M74
    ("France", "Sweden"),                         # M77
    ("South Africa", "Canada"),                   # M73
    ("Netherlands", "Morocco"),                   # M75
    ("Colombia", "Ghana"),                        # M83
    ("Spain", "Austria"),                         # M84
    ("United States", "Bosnia and Herzegovina"),  # M81
    ("Belgium", "Senegal"),                       # M82
    ("Brazil", "Japan"),                          # M76
    ("Cote d'Ivoire", "Norway"),                  # M78
    ("Mexico", "Ecuador"),                        # M79
    ("England", "DR Congo"),                      # M80
    ("Argentina", "Cabo Verde"),                  # M86
    ("Australia", "Egypt"),                       # M88
    ("Switzerland", "Algeria"),                   # M85
    ("Portugal", "Croatia"),                      # M87
]

ROUND_NAMES = ['1/16 финала', '1/8 финала', '1/4 финала', '1/2 финала', 'ФИНАЛ']
ROUND_POINTS = {0: 1, 1: 1.5, 2: 2, 3: 3, 4: 5}
ROUND_SIZES = [16, 8, 4, 2, 1]
OFFSETS = [0, 16, 24, 28, 30]
TOTAL = 31                      # matches: 16+8+4+2+1

def round_of(idx):
    if idx < 16: return 0
    if idx < 24: return 1
    if idx < 28: return 2
    if idx < 30: return 3
    return 4

def feeders(idx):
    """Global match indices that feed match idx (None for Round of 32)."""
    r = round_of(idx)
    if r == 0:
        return None
    pos = idx - OFFSETS[r]
    prev = OFFSETS[r - 1]
    return (prev + 2 * pos, prev + 2 * pos + 1)

def teams_of(idx, picks):
    """Two teams playing match idx, given picks {match_index: winner_team}."""
    if idx < 16:
        return R32_PAIRS[idx]
    f0, f1 = feeders(idx)
    return picks.get(f0), picks.get(f1)

def champion(picks):
    return picks.get(30)
