# -*- coding: utf-8 -*-
"""Google Sheets data layer. Stores player submissions and the admin's actual results.
Falls back to a local JSON file if no Google creds are configured (handy for testing)."""
import os, json, time

SPREADSHEET_ID = os.getenv('SPREADSHEET_ID', '')
GOOGLE_CREDS = os.getenv('GOOGLE_CREDENTIALS_JSON', '')   # the service-account JSON (whole string)
LOCAL_DB = os.getenv('LOCAL_DB', 'local_db.json')

_gc = None
_ws_players = None
_ws_state = None

def _use_sheets():
    return bool(SPREADSHEET_ID and GOOGLE_CREDS)

def _connect():
    global _gc, _ws_players, _ws_state
    if _gc is not None:
        return
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(GOOGLE_CREDS)
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    _gc = gspread.authorize(creds)
    sh = _gc.open_by_key(SPREADSHEET_ID)
    def tab(name, header):
        try:
            w = sh.worksheet(name)
        except Exception:
            w = sh.add_worksheet(title=name, rows=200, cols=6)
            w.append_row(header)
        return w
    _ws_players = tab('players', ['user_id', 'name', 'submission_json', 'updated_at'])
    _ws_state = tab('state', ['key', 'value'])

# ---------- local fallback ----------
def _load_local():
    if os.path.exists(LOCAL_DB):
        return json.load(open(LOCAL_DB, encoding='utf-8'))
    return {'players': {}, 'state': {}}

def _save_local(db):
    json.dump(db, open(LOCAL_DB, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# ---------- public API ----------
def save_submission(user_id, name, data):
    payload = json.dumps(data, ensure_ascii=False)
    if _use_sheets():
        _connect()
        cell = _ws_players.find(str(user_id), in_column=1)
        row = [str(user_id), name, payload, time.strftime('%Y-%m-%d %H:%M')]
        if cell:
            _ws_players.update(f'A{cell.row}:D{cell.row}', [row])
        else:
            _ws_players.append_row(row)
    else:
        db = _load_local()
        db['players'][str(user_id)] = {'name': name, 'submission': data,
                                       'updated_at': time.strftime('%Y-%m-%d %H:%M')}
        _save_local(db)

def get_submission(user_id):
    if _use_sheets():
        _connect()
        cell = _ws_players.find(str(user_id), in_column=1)
        if not cell:
            return None
        row = _ws_players.row_values(cell.row)
        return {'name': row[1], 'submission': json.loads(row[2])}
    db = _load_local()
    return db['players'].get(str(user_id))

def clear_submissions():
    """Wipe all player predictions (keeps results/deadline)."""
    if _use_sheets():
        _connect()
        _ws_players.clear()
        _ws_players.append_row(['user_id', 'name', 'submission_json', 'updated_at'])
    else:
        db = _load_local(); db['players'] = {}; _save_local(db)

def all_submissions():
    """Returns {name: submission_dict}."""
    out = {}
    if _use_sheets():
        _connect()
        for row in _ws_players.get_all_values()[1:]:
            if len(row) >= 3 and row[2]:
                out[row[1]] = json.loads(row[2])
    else:
        for p in _load_local()['players'].values():
            out[p['name']] = p['submission']
    return out

def _state_get(key, default=None):
    if _use_sheets():
        _connect()
        cell = _ws_state.find(key, in_column=1)
        return _ws_state.cell(cell.row, 2).value if cell else default
    return _load_local()['state'].get(key, default)

def _state_set(key, value):
    if _use_sheets():
        _connect()
        cell = _ws_state.find(key, in_column=1)
        if cell:
            _ws_state.update_cell(cell.row, 2, value)
        else:
            _ws_state.append_row([key, value])
    else:
        db = _load_local(); db['state'][key] = value; _save_local(db)

def get_actual():
    raw = _state_get('actual', '')
    return json.loads(raw) if raw else {'ko': {}}

def set_actual(actual):
    _state_set('actual', json.dumps({'ko': actual.get('ko', {})}, ensure_ascii=False))

def set_ko_result(match_no, home, away, winner, hs, as_):
    a = get_actual(); a.setdefault('ko', {})[str(match_no)] = {
        'home': home, 'away': away, 'w': winner, 'hs': hs, 'as': as_}
    _state_set('actual', json.dumps(a, ensure_ascii=False))

def set_ko_winner(match_no, team):
    a = get_actual(); cur = a.setdefault('ko', {}).setdefault(str(match_no), {})
    cur['w'] = team
    _state_set('actual', json.dumps(a, ensure_ascii=False))

def get_facts():
    raw = _state_get('facts', '')
    return json.loads(raw) if raw else {}

def set_facts(facts):
    _state_set('facts', json.dumps(facts, ensure_ascii=False))

def get_deadline():
    return _state_get('deadline', '')

def set_deadline(value):
    _state_set('deadline', value)

def get_winners():
    raw = _state_get('winners', '')
    return json.loads(raw) if raw else {}

def set_winner(idx, team):
    w = get_winners(); w[str(idx)] = team
    _state_set('winners', json.dumps(w, ensure_ascii=False))

def set_winners_bulk(d):
    w = get_winners(); w.update({str(k): v for k, v in d.items()})
    _state_set('winners', json.dumps(w, ensure_ascii=False))
