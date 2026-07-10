# -*- coding: utf-8 -*-
"""WC 2026 Playoff Predictor — in-chat buttons + automatic results.
v3 fixes & UX:
  * penalty shootouts resolve correctly (score.winner / penalties from API)
  * robust team-name matching (Cote d'Ivoire, DR Congo, Bosnia...)
  * sync every SYNC_EVERY_H hours (default 2) — not once a day
  * instant result cards posted to the group as soon as a match is decided
  * redesigned digest: flags, scores & pens, who-guessed counts, leaderboard
    with movement arrows, champion-alive block, upcoming matches with pick split
  * /diag for one-tap health check"""
import os, re, asyncio, logging, datetime as dt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, ContextTypes)

import sheets, bracket, fc26
try:
    import results_api
except Exception:
    results_api = None

BOT_TOKEN = os.environ['BOT_TOKEN']
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x.strip().isdigit()}
GROUP_CHAT_ID = os.getenv('GROUP_CHAT_ID', '')
FD_TOKEN = os.getenv('FOOTBALL_DATA_TOKEN', '')
SYNC_HOUR = int(os.getenv('SYNC_HOUR', '10'))          # kept for compatibility
POST_HOUR = int(os.getenv('POST_HOUR', '11'))
TZ_OFFSET = int(os.getenv('TZ_OFFSET', '5'))            # Almaty UTC+5
SYNC_EVERY_H = int(os.getenv('SYNC_EVERY_H', '2'))      # NEW: sync cadence, hours
# Fair late (re)submissions: entries updated AFTER this moment (UTC 'YYYY-MM-DD HH:MM')
# earn points only for matches that kicked off AFTER their submission time.
LATE_CUTOFF = os.getenv('LATE_CUTOFF', '')
WINDOW_HOURS = int(os.getenv('DIGEST_HOURS', '20'))
ANNOUNCE = os.getenv('ANNOUNCE_RESULTS', '1') != '0'    # instant result cards to the group

ROUND_TAG = {0: '1/16', 1: '1/8', 2: '1/4', 3: '1/2', 4: 'Финал'}

# ---- «Угадай счёт» (score game, from R16 on) ----
SP_EXACT, SP_OUTCOME, SP_DRAW = 3.0, 1.0, 3.0
SP_SCORES = ['10', '20', '21', '31']            # winner-oriented score options
SP_FROM_IDX = 16                                # R16 onward
SP_POST_HOUR = int(os.getenv('SP_POST_HOUR', '11'))   # Almaty: cards open daily at 11:00
SP_HORIZON_H = int(os.getenv('SP_HORIZON_H', '21'))   # covers matches 21:00 tonight … 07:00 tomorrow
FLAGS = {
 'Germany': '🇩🇪', 'Paraguay': '🇵🇾', 'France': '🇫🇷', 'Sweden': '🇸🇪',
 'South Africa': '🇿🇦', 'Canada': '🇨🇦', 'Netherlands': '🇳🇱', 'Morocco': '🇲🇦',
 'Portugal': '🇵🇹', 'Croatia': '🇭🇷', 'Spain': '🇪🇸', 'Austria': '🇦🇹',
 'United States': '🇺🇸', 'Bosnia and Herzegovina': '🇧🇦', 'Belgium': '🇧🇪', 'Senegal': '🇸🇳',
 'Brazil': '🇧🇷', 'Japan': '🇯🇵', "Cote d'Ivoire": '🇨🇮', 'Norway': '🇳🇴',
 'Mexico': '🇲🇽', 'Ecuador': '🇪🇨', 'England': '🏴󠁧󠁢󠁥󠁮󠁧󠁿', 'DR Congo': '🇨🇩',
 'Argentina': '🇦🇷', 'Cabo Verde': '🇨🇻', 'Australia': '🇦🇺', 'Egypt': '🇪🇬',
 'Switzerland': '🇨🇭', 'Algeria': '🇩🇿', 'Colombia': '🇨🇴', 'Ghana': '🇬🇭',
}

def T(team):
    """Team with flag."""
    return f"{FLAGS.get(team, '')} {team}".strip()

def is_admin(uid):
    return uid in ADMIN_IDS

def _utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

def _deadline_passed():
    d = sheets.get_deadline()
    if not d:
        return False
    try:
        return _utcnow() + dt.timedelta(hours=TZ_OFFSET) > dt.datetime.strptime(d, '%Y-%m-%d %H:%M')
    except ValueError:
        return False

def canon(s):
    """Aggressive normalization so \"Cote d'Ivoire\" == 'Cote d Ivoire' == 'Côte d’Ivoire'."""
    s = (s or '').lower()
    s = (s.replace('ô', 'o').replace('é', 'e').replace('ç', 'c').replace('ü', 'u')
          .replace('ö', 'o').replace('ã', 'a').replace('í', 'i').replace('’', ''))
    s = re.sub(r'[^a-z]', '', s)
    s = s.replace('congodr', 'drcongo').replace('capeverdeislands', 'caboverde')
    s = s.replace('capeverde', 'caboverde').replace('ivorycoast', 'cotedivoire')
    s = s.replace('turkey', 'turkiye').replace('czechrepublic', 'czechia')
    s = s.replace('korearepublic', 'southkorea').replace('unitedstatesofamerica', 'unitedstates')
    s = s.replace('iriran', 'iran')
    return s

def same_team(x, y):
    """Tolerant equality: exact canon match, or one canon name is a prefix/superset
    of the other (>=5 chars) — survives API spelling variants we haven't seen yet."""
    cx, cy = canon(x), canon(y)
    if not cx or not cy:
        return False
    if cx == cy:
        return True
    if len(cx) >= 5 and len(cy) >= 5 and (cx.startswith(cy) or cy.startswith(cx)
                                          or cx in cy or cy in cx):
        return True
    return False

def pair_matches(m, t0, t1):
    return ((same_team(m['home'], t0) and same_team(m['away'], t1)) or
            (same_team(m['home'], t1) and same_team(m['away'], t0)))

# ======================= prediction flow (buttons) =======================
async def start(update: Update, ctx):
    await update.message.reply_text(
        '⚽ Прогноз плей-офф ЧМ-2026! Жми, кто проходит дальше — и так до чемпиона.\n'
        'Переделать — /restart · мой прогноз — /mybracket · таблица — /leaderboard')
    await _begin(update, ctx)

async def _send_match(chat_id, ctx):
    idx = ctx.user_data['idx']; picks = ctx.user_data['picks']
    a, b = bracket.teams_of(idx, picks)
    rn = bracket.ROUND_NAMES[bracket.round_of(idx)]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f'✅ {a}', callback_data=f'p:{idx}:0')],
                               [InlineKeyboardButton(f'✅ {b}', callback_data=f'p:{idx}:1')]])
    await ctx.bot.send_message(chat_id, f'<b>{rn}</b> · матч {idx + 1}/{bracket.TOTAL}\n\n'
                               f'Кто проходит дальше?\n\n🆚 <b>{a}</b>  —  <b>{b}</b>',
                               parse_mode='HTML', reply_markup=kb)

async def _begin(update: Update, ctx):
    if _deadline_passed():
        await ctx.bot.send_message(update.effective_chat.id, '⛔ Приём прогнозов уже закрыт.')
        return
    ctx.user_data['idx'] = 0; ctx.user_data['picks'] = {}
    await _send_match(update.effective_chat.id, ctx)

async def on_callback(update: Update, ctx):
    q = update.callback_query
    if q.data == 'go':
        await q.answer(); await _begin(update, ctx); return
    if q.data.startswith('fc:'):
        await _on_fc_tap(update, ctx); return
    if q.data.startswith('spt:'):
        await q.answer('🧪 Это тест-превью — голос не считается. В группе кнопки будут работать.',
                       show_alert=False)
        return
    if q.data.startswith('sp:'):
        await _on_sp_tap(update, ctx); return
    if q.data.startswith('p:'):
        _, sidx, choice = q.data.split(':'); idx = int(sidx)
        if ctx.user_data.get('idx') is None:
            await q.answer('Начни заново: /start', show_alert=True); return
        if idx != ctx.user_data['idx']:
            await q.answer('Это уже прошедший матч.'); return
        picks = ctx.user_data['picks']
        a, b = bracket.teams_of(idx, picks)
        winner = a if choice == '0' else b
        picks[idx] = winner
        rn = bracket.ROUND_NAMES[bracket.round_of(idx)]
        await q.answer(f'➡️ {winner}')
        await q.edit_message_text(f'<b>{rn}</b> · матч {idx + 1}/{bracket.TOTAL}\n✅ Проходит: <b>{winner}</b>',
                                  parse_mode='HTML')
        if idx < bracket.TOTAL - 1:
            ctx.user_data['idx'] = idx + 1
            await _send_match(q.message.chat_id, ctx)
        else:
            await _finish(update, ctx)

async def _finish(update: Update, ctx):
    picks = ctx.user_data['picks']
    sheets.save_submission(update.effective_user.id, update.effective_user.full_name,
                           {'picks': {str(k): v for k, v in picks.items()}, 'champion': bracket.champion(picks)})
    ctx.user_data['idx'] = None
    await ctx.bot.send_message(update.effective_chat.id,
                               f'🏆 Готово!\nТвой чемпион: <b>{bracket.champion(picks)}</b>\n\n'
                               'Изменить — /restart · посмотреть — /mybracket',
                               parse_mode='HTML')

async def restart(update: Update, ctx):
    await _begin(update, ctx)

async def mybracket(update: Update, ctx):
    sub = sheets.get_submission(update.effective_user.id)
    if not sub or not sub['submission'].get('picks'):
        await update.message.reply_text('У тебя пока нет прогноза. Нажми /start.')
        return
    picks = {int(k): v for k, v in sub['submission']['picks'].items()}
    won = sheets.get_winners()
    lines = ['📋 Твой прогноз:']
    for r, nm in enumerate(bracket.ROUND_NAMES):
        ws = []
        for i in range(bracket.OFFSETS[r], bracket.OFFSETS[r] + bracket.ROUND_SIZES[r]):
            if i not in picks:
                continue
            mark = ''
            if str(i) in won and won[str(i)]:
                mark = ' ✅' if canon(won[str(i)]) == canon(picks[i]) else ' ❌'
            ws.append(picks[i] + mark)
        if ws:
            lines.append(f'<b>{nm}</b>: ' + ', '.join(ws))
    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')

# ======================= score game («Угадай счёт») =======================
def _json_state(key, default):
    import json as _j
    raw = sheets._state_get(key, '')
    try:
        return _j.loads(raw) if raw else default
    except Exception:
        return default

def _set_json_state(key, val):
    import json as _j
    sheets._state_set(key, _j.dumps(val, ensure_ascii=False))

def _sp_all():
    """{idx: {uid: {'n': name, 't': 'h'/'a'/'d', 's': '21'}}}"""
    return _json_state('sp', {})

def _sp_meta():
    """{idx: {'home','away','date','msg'(chat msg id)}} for posted cards."""
    return _json_state('spmeta', {})

def _uid_display_name(uid, fallback):
    """Prefer the name from the players sheet so points merge into one row."""
    try:
        rows = sheets.player_rows_all()
        if rows:
            for r in rows:
                if r and r[0] == str(uid) and len(r) > 1 and r[1]:
                    return r[1]
    except Exception:
        pass
    return fallback

def _sp_points(pred, d):
    """Points for one score prediction given decided koinfo d."""
    dur = d.get('dur'); w = d.get('w'); hs, as_ = d.get('hs'), d.get('as')
    if not w:
        return 0.0
    draw90 = dur in ('PENALTY_SHOOTOUT', 'EXTRA_TIME')
    if pred.get('t') == 'd':
        return SP_DRAW if draw90 else 0.0
    team = d.get('home') if pred.get('t') == 'h' else d.get('away')
    if canon(team) != canon(w):
        return 0.0
    if draw90 or hs is None:
        return SP_OUTCOME
    ws, ls = (hs, as_) if canon(w) == canon(d.get('home')) else (as_, hs)
    return SP_EXACT if f'{ws}{ls}' == pred.get('s') else SP_OUTCOME

def _sp_totals():
    """{name: score_game_points} over all decided matches."""
    sp = _sp_all(); meta = _sp_meta(); koinfo = sheets.get_koinfo()
    totals = {}
    for sidx, preds in sp.items():
        d = koinfo.get(sidx)
        if not d or not d.get('w'):
            continue
        for uid, p in preds.items():
            pts = _sp_points(p, d)
            if pts:
                nm = p.get('n', uid)
                totals[nm] = totals.get(nm, 0.0) + pts
    return totals

def _sp_card_text(idx, h, a, date_utc, sp=None):
    t_loc = ''
    try:
        t = dt.datetime.strptime(date_utc, '%Y-%m-%dT%H:%M:%SZ')
        t_loc = (t + dt.timedelta(hours=TZ_OFFSET)).strftime('%d.%m %H:%M')
    except Exception:
        pass
    tag = ROUND_TAG.get(bracket.round_of(idx), '')
    lines = [f'🎯 <b>УГАДАЙ СЧЁТ · {tag}</b>',
             f'{T(h)}  🆚  {T(a)}',
             f'🕘 {t_loc} (Алматы) · приём до стартового свистка',
             '',
             'Точный счёт <b>+3</b> · исход <b>+1</b> · ничья в 90 мин <b>+3</b>']
    preds = (sp or _sp_all()).get(str(idx), {})
    if preds:
        nh = sum(1 for p in preds.values() if p.get('t') == 'h')
        na = sum(1 for p in preds.values() if p.get('t') == 'a')
        nd = sum(1 for p in preds.values() if p.get('t') == 'd')
        lines.append(f'📊 Голоса: {h} {nh} · ничья {nd} · {a} {na}')
    return '\n'.join(lines)

def _sp_keyboard(idx, h, a, test=False):
    pref = 'spt' if test else 'sp'
    def row(side, team):
        return [InlineKeyboardButton(f'{FLAGS.get(team, "")} {s[0]}:{s[1]}',
                                     callback_data=f'{pref}:{idx}:{side}:{s}') for s in SP_SCORES]
    return InlineKeyboardMarkup([
        row('h', h),
        row('a', a),
        [InlineKeyboardButton('🤝 Ничья в 90 мин (пенальти)', callback_data=f'{pref}:{idx}:d:00')],
    ])

async def _post_sp_cards(ctx, chat_id, horizon_h=SP_HORIZON_H, test=False):
    """Post prediction cards for bracket matches kicking off within horizon.
    test=True: preview only — nothing saved, taps don't count."""
    ups = _json_state('upcoming', [])
    meta = _sp_meta()
    now = _utcnow(); posted = 0
    for u in ups:
        idx = u['idx']
        if idx < SP_FROM_IDX or (not test and str(idx) in meta):
            continue
        try:
            t = dt.datetime.strptime(u['date'], '%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            continue
        if not (now <= t <= now + dt.timedelta(hours=horizon_h)):
            continue
        txt = _sp_card_text(idx, u['home'], u['away'], u['date'])
        if test:
            txt = '🧪 <b>ТЕСТ-ПРЕВЬЮ</b> (в группу не уходит, тапы не считаются)\n\n' + txt
        msg = await ctx.bot.send_message(chat_id, txt, parse_mode='HTML',
                                         reply_markup=_sp_keyboard(idx, u['home'], u['away'], test=test))
        if not test:
            meta[str(idx)] = {'home': u['home'], 'away': u['away'], 'date': u['date'],
                              'chat': msg.chat_id, 'msg': msg.message_id}
        posted += 1
    if posted and not test:
        _set_json_state('spmeta', meta)
    return posted

async def sp_post_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    try:
        await _post_sp_cards(ctx, GROUP_CHAT_ID)
    except Exception:
        logging.exception('sp_post_job failed')

async def spost_cmd(update: Update, ctx):
    """Admin: /spost — open cards in the group; /spost test — private preview of real
    upcoming cards; /spost demo — fake demo card right now (buttons work, nothing saved)."""
    if not is_admin(update.effective_user.id):
        return
    arg = (ctx.args[0].lower() if ctx.args else '')
    if arg == 'demo':
        h, a = 'Paraguay', 'France'
        date = (_utcnow() + dt.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        txt = ('🧪 <b>ДЕМО</b> — так карточка будет выглядеть в группе с 1/8 финала.\n'
               'Кнопки живые, но голос не считается.\n\n' + _sp_card_text(16, h, a, date))
        await ctx.bot.send_message(update.effective_chat.id, txt, parse_mode='HTML',
                                   reply_markup=_sp_keyboard(16, h, a, test=True))
        return
    test = arg == 'test'
    target = update.effective_chat.id if test else (GROUP_CHAT_ID or update.effective_chat.id)
    n = await _post_sp_cards(ctx, target, horizon_h=30, test=test)
    tail = ' (тест — только тебе)' if test else ''
    await update.message.reply_text(
        f'✅ Карточек: {n}{tail}' if n else
        'Пока нет матчей 1/8+ в ближайшие 30ч — карточки откроются сами в 11:00 в день матча.\n'
        'Посмотреть, как это выглядит: /spost demo')

async def _on_sp_tap(update: Update, ctx):
    q = update.callback_query
    try:
        _, sidx, side, s = q.data.split(':')
        idx = int(sidx)
    except Exception:
        await q.answer(); return
    meta = _sp_meta().get(sidx)
    if not meta:
        await q.answer('Этот матч уже закрыт.', show_alert=True); return
    try:
        kickoff = dt.datetime.strptime(meta['date'], '%Y-%m-%dT%H:%M:%SZ')
        if _utcnow() >= kickoff:
            await q.answer('⛔ Матч уже начался — приём закрыт.', show_alert=True); return
    except Exception:
        pass
    uid = q.from_user.id
    name = _uid_display_name(uid, q.from_user.full_name)
    sp = _sp_all()
    sp.setdefault(sidx, {})[str(uid)] = {'n': name, 't': side, 's': s}
    _set_json_state('sp', sp)
    if side == 'd':
        human = 'ничья в 90 мин (пенальти)'
    else:
        team = meta['home'] if side == 'h' else meta['away']
        human = f'{team} {s[0]}:{s[1]}'
    await q.answer(f'✅ Принято: {human}')
    try:
        await q.edit_message_text(_sp_card_text(idx, meta['home'], meta['away'], meta['date'], sp=sp),
                                  parse_mode='HTML',
                                  reply_markup=_sp_keyboard(idx, meta['home'], meta['away']))
    except Exception:
        pass   # unchanged text / rate limit — not critical

def _sp_award_lines(idx, d):
    """Result lines for the score game: exact-score heroes + outcome count."""
    preds = _sp_all().get(str(idx), {})
    if not preds:
        return []
    exact, outcome, draw_heroes = [], 0, []
    for uid, p in preds.items():
        pts = _sp_points(p, d)
        if pts == SP_EXACT and p.get('t') != 'd':
            exact.append(p.get('n', uid))
        elif pts == SP_DRAW and p.get('t') == 'd':
            draw_heroes.append(p.get('n', uid))
        elif pts == SP_OUTCOME:
            outcome += 1
    out = []
    if exact:
        out.append(f'💎 Точный счёт (+{SP_EXACT:g}): <b>{", ".join(sorted(exact))}</b>')
    if draw_heroes:
        out.append(f'🤝 Угадали ничью (+{SP_DRAW:g}): <b>{", ".join(sorted(draw_heroes))}</b>')
    if outcome:
        out.append(f'✅ Угадали исход (+{SP_OUTCOME:g}): {outcome} чел.')
    return out

# ======================= scoring / leaderboard =======================
def _score(picks, actual, cutoff=None, koinfo=None):
    """cutoff (datetime): count a position only if its match kicked off after cutoff —
    fair scoring for players who (re)submitted late."""
    total = 0.0; correct = 0
    for k, team in picks.items():
        i = int(k)
        w = actual.get(str(i))
        if not w or canon(w) != canon(team):
            continue
        if cutoff is not None:
            ds = ((koinfo or {}).get(str(i)) or {}).get('date')
            if ds:
                try:
                    if dt.datetime.strptime(ds, '%Y-%m-%dT%H:%M:%SZ') < cutoff:
                        continue        # match started before they submitted — no points
                except Exception:
                    pass
        total += bracket.ROUND_POINTS[bracket.round_of(i)]; correct += 1
    return total, correct

def _submit_times():
    """{name: 'YYYY-MM-DD HH:MM'} from the players tab (updated_at, UTC)."""
    out = {}
    try:
        rows = sheets.player_rows_all()
        if rows:
            for r in rows:
                if r and len(r) >= 4 and r[1] and r[3]:
                    out[r[1]] = r[3]
    except Exception:
        pass
    return out

def _parse_dt(s):
    """Accepts 'YYYY-MM-DD HH:MM' or bare 'YYYY-MM-DD'."""
    s = (s or '').strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def _late_cutoff_for(name, times):
    """Returns the player's own submission datetime if it is later than LATE_CUTOFF."""
    if not LATE_CUTOFF:
        return None
    gate = _parse_dt(LATE_CUTOFF)
    sub_t = _parse_dt(times.get(name, ''))
    if gate is None or sub_t is None:
        return None
    return sub_t if sub_t > gate else None

def _leaderboard():
    """Rows: (name, total, correct, bracket_pts, score_game_pts)."""
    actual = sheets.get_winners()
    subs = sheets.all_submissions()
    spt = _sp_totals()
    koinfo = sheets.get_koinfo() if LATE_CUTOFF else None
    times = _submit_times() if LATE_CUTOFF else {}
    rows = []
    seen = set()
    for name, sub in subs.items():
        br, c = _score(sub.get('picks', {}), actual,
                       cutoff=_late_cutoff_for(name, times), koinfo=koinfo)
        sg = spt.get(name, 0.0)
        rows.append((name, br + sg, c, br, sg)); seen.add(name)
    for name, sg in spt.items():            # score-game players without a bracket
        if name not in seen:
            rows.append((name, sg, 0, 0.0, sg))
    rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
    return rows

def _prev_ranks():
    return _json_state('lbprev', {})

def _store_ranks(rows):
    _set_json_state('lbprev', {r[0]: i for i, r in enumerate(rows, 1)})

def _arrow(name, cur_rank, prev):
    p = prev.get(name)
    if p is None or p == cur_rank:
        return ''
    return f' ⬆️{p - cur_rank}' if p > cur_rank else f' ⬇️{cur_rank - p}'

def _fmt_lb(rows, top=10, arrows=True, detail=False):
    medals = {1: '🥇', 2: '🥈', 3: '🥉'}
    prev = _prev_ranks() if arrows else {}
    out = ['🏆 <b>Таблица лидеров</b>']
    for i, r in enumerate(rows[:top], 1):
        name, t = r[0], r[1]
        extra = ''
        if detail and len(r) >= 5 and r[4]:
            extra = f' <i>(сетка {r[3]:g} + счёт {r[4]:g})</i>'
        out.append(f"{medals.get(i, str(i)+'.')} {name} — <b>{t:g}</b>{_arrow(name, i, prev)}{extra}")
    rest = len(rows) - top
    if rest > 0:
        out.append(f'<i>…и ещё {rest} — полная таблица: /leaderboard</i>')
    return '\n'.join(out) if len(out) > 1 else '🏆 Пока нет прогнозов.'

async def leaderboard_cmd(update: Update, ctx):
    rows = _leaderboard()
    msg = _fmt_lb(rows, top=len(rows) or 1, detail=True)
    recent = _recent_ko()
    g = _top_gainers_text(recent) if recent else None
    if g:
        msg += '\n\n' + g
    await update.message.reply_text(msg, parse_mode='HTML')

# ======================= results / digest =======================
def _guessed(idx, winner):
    """How many players picked `winner` at bracket position idx."""
    subs = sheets.all_submissions()
    n = sum(1 for _, sub in subs.items()
            if canon(sub.get('picks', {}).get(str(idx), '')) == canon(winner))
    return n, len(subs)

def _result_line(idx, d, with_guessed=True):
    tag = ROUND_TAG.get(bracket.round_of(idx), '')
    w = d.get('w', '')
    if d.get('hs') is not None:
        pens = ''
        if d.get('ph') is not None and d.get('pa') is not None:
            pens = f" · пен. {d['ph']}–{d['pa']}"
        elif d.get('dur') == 'PENALTY_SHOOTOUT':
            pens = ' · по пенальти'
        elif d.get('dur') == 'EXTRA_TIME':
            pens = ' · доп. время'
        line = f"{tag} · {T(d['home'])} <b>{d['hs']}–{d['as']}</b> {T(d['away'])}{pens} → 🏆 <b>{w}</b>"
    else:
        line = f"{tag} · 🏆 <b>{T(w)}</b>"
    if with_guessed and w:
        n, tot = _guessed(idx, w)
        line += f'\n      🎯 угадали: {n}/{tot}'
    return line

def _recent_ko(hours=WINDOW_HOURS):
    info = sheets.get_koinfo()
    cutoff = _utcnow() - dt.timedelta(hours=hours)
    recent = {}
    for k, d in info.items():
        ds = d.get('date')
        if not ds or not d.get('w'):
            continue
        try:
            t = dt.datetime.strptime(ds, '%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            continue
        if t >= cutoff:
            recent[int(k)] = d
    return recent

def _playoff_results_text(recent, hours=WINDOW_HOURS):
    if not recent:
        return None
    lines = [f'⚽ <b>Результаты за {hours}ч</b>']
    for idx in sorted(recent):
        lines.append(_result_line(idx, recent[idx]))
    return '\n'.join(lines)

def _top_gainers_text(recent, hours=WINDOW_HOURS, top=3):
    if not recent:
        return None
    gains = []
    for name, sub in sheets.all_submissions().items():
        picks = sub.get('picks', {}); g = 0.0; c = 0
        for idx, d in recent.items():
            if canon(picks.get(str(idx), '')) == canon(d.get('w', '')):
                g += bracket.ROUND_POINTS[bracket.round_of(idx)]; c += 1
        if g > 0:
            gains.append((name, g, c))
    if not gains:
        return None
    gains.sort(key=lambda r: (-r[1], -r[2], r[0]))
    medals = {1: '🥇', 2: '🥈', 3: '🥉'}
    lines = [f'🔥 <b>Лучшие за {hours}ч</b>']
    for i, (name, g, c) in enumerate(gains[:top], 1):
        lines.append(f"{medals.get(i, str(i)+'.')} {name} +{g:g}")
    return '\n'.join(lines)

def _playoff_overall_text():
    won = sheets.get_winners()
    if not won:
        return None
    koinfo = sheets.get_koinfo()
    lines = ['⚽ <b>Плей-офф — сыграно</b>']
    for idx in sorted(int(k) for k in won if won[k]):
        d = dict(koinfo.get(str(idx)) or {})
        d.setdefault('w', won[str(idx)])
        lines.append(_result_line(idx, d, with_guessed=False))
    return '\n'.join(lines)

def _eliminated():
    """Set of canon() team names knocked out (loser of every decided KO match)."""
    out = set()
    for k, d in sheets.get_koinfo().items():
        w, h, a = d.get('w'), d.get('home'), d.get('away')
        if w and h and a:
            out.add(canon(a) if canon(w) == canon(h) else canon(h))
    return out

def _champion_block():
    subs = sheets.all_submissions()
    if not subs:
        return None
    dead = _eliminated()
    alive, lost = {}, {}
    for _, sub in subs.items():
        ch = sub.get('champion') or sub.get('picks', {}).get('30')
        if not ch:
            continue
        bucket = lost if canon(ch) in dead else alive
        bucket[ch] = bucket.get(ch, 0) + 1
    if not alive and not lost:
        return None
    parts = []
    if alive:
        top = sorted(alive.items(), key=lambda kv: -kv[1])
        parts.append('👑 <b>Чемпионские ставки живы:</b> ' +
                     ' · '.join(f'{T(t)} — {n}' for t, n in top[:5]))
    if lost:
        gone = sorted(lost.items(), key=lambda kv: -kv[1])
        parts.append('💀 <b>Чемпион уже выбыл у:</b> ' +
                     ' · '.join(f'{T(t)} — {n}' for t, n in gone[:5]))
    return '\n'.join(parts)

def _upcoming_block(hours=36):
    import json as _j
    raw = sheets._state_get('upcoming', '')
    try:
        ups = _j.loads(raw) if raw else []
    except Exception:
        ups = []
    if not ups:
        return None
    now = _utcnow(); horizon = now + dt.timedelta(hours=hours)
    subs = sheets.all_submissions()
    lines = []
    for u in ups:
        try:
            t = dt.datetime.strptime(u['date'], '%Y-%m-%dT%H:%M:%SZ')
        except Exception:
            continue
        if not (now - dt.timedelta(hours=3) <= t <= horizon):
            continue
        idx = u['idx']; h, a = u['home'], u['away']
        nh = sum(1 for s in subs.values() if canon(s.get('picks', {}).get(str(idx), '')) == canon(h))
        na = sum(1 for s in subs.values() if canon(s.get('picks', {}).get(str(idx), '')) == canon(a))
        loc = (t + dt.timedelta(hours=TZ_OFFSET)).strftime('%d.%m %H:%M')
        lines.append(f"{ROUND_TAG.get(bracket.round_of(idx),'')} · {T(h)} vs {T(a)} · {loc}"
                     f"\n      голоса группы: {nh}–{na}")
    if not lines:
        return None
    return '📅 <b>Ближайшие матчи</b> (время Алматы)\n' + '\n'.join(lines)

def _digest_text(header):
    rows = _leaderboard()
    parts = [header + _fmt_lb(rows, top=10)]
    recent = _recent_ko()
    if recent:
        parts.append(_playoff_results_text(recent))
        gain = _top_gainers_text(recent)
        if gain:
            parts.append(gain)
    else:
        fb = _playoff_overall_text()
        if fb:
            parts.append(fb)
    ch = _champion_block()
    if ch:
        parts.append(ch)
    up = _upcoming_block()
    if up:
        parts.append(up)
    return '\n\n'.join(p for p in parts if p)

# ======================= sync =======================
def _resolve_actual(matches):
    """Map real DECIDED matches onto the 31 bracket positions.
    Handles extra time and penalty shootouts via score.winner/penalties."""
    fin = [m for m in matches if m.get('status') == 'FINISHED' or
           (m.get('status') is None and m.get('hs') is not None and m.get('as') is not None)]
    def find(t0, t1):
        for m in fin:
            if pair_matches(m, t0, t1):
                return m
        return None
    def decided_winner(m):
        if results_api is not None:
            return results_api.match_winner(m)
        if m['hs'] is None or m['as'] is None:
            return None
        if m['hs'] > m['as']: return m['home']
        if m['as'] > m['hs']: return m['away']
        return None
    info = {}
    for idx in range(bracket.TOTAL):
        if idx < 16:
            t0, t1 = bracket.R32_PAIRS[idx]
        else:
            f0, f1 = bracket.feeders(idx)
            t0 = info.get(f0, {}).get('w'); t1 = info.get(f1, {}).get('w')
        if not t0 or not t1:
            continue
        m = find(t0, t1)
        if not m:
            continue
        w = decided_winner(m)
        if not w:
            continue
        home_is_t0 = same_team(m['home'], t0)
        info[idx] = {'w': t0 if same_team(w, t0) else t1,
                     'home': t0 if home_is_t0 else t1,
                     'away': t1 if home_is_t0 else t0,
                     'hs': m['hs'] if home_is_t0 else m['as'],
                     'as': m['as'] if home_is_t0 else m['hs'],
                     'ph': (m.get('ph') if home_is_t0 else m.get('pa')),
                     'pa': (m.get('pa') if home_is_t0 else m.get('ph')),
                     'dur': m.get('duration'), 'date': m.get('utcDate')}
    return info

def _upcoming_from(matches, info):
    """Scheduled matches that map onto bracket positions whose teams are known."""
    ups = []
    sched = [m for m in matches if m.get('status') in ('TIMED', 'SCHEDULED', 'IN_PLAY', 'PAUSED')]
    for idx in range(bracket.TOTAL):
        if idx in info:
            continue
        if idx < 16:
            t0, t1 = bracket.R32_PAIRS[idx]
        else:
            f0, f1 = bracket.feeders(idx)
            t0 = info.get(f0, {}).get('w'); t1 = info.get(f1, {}).get('w')
        if not t0 or not t1:
            continue
        for m in sched:
            if pair_matches(m, t0, t1) and m.get('utcDate'):
                ups.append({'idx': idx, 'home': t0, 'away': t1, 'date': m['utcDate']})
                break
    return ups

async def _do_sync(ctx):
    """Returns (matches, newly_decided_info) or (None, {})."""
    if not (FD_TOKEN and results_api):
        return None, {}
    loop = asyncio.get_event_loop()
    matches = await loop.run_in_executor(None, results_api.fetch_sync, FD_TOKEN)
    built = results_api.build_actual(matches)
    sheets.set_facts({'standings': built.get('standings', {}), 'tables': built.get('tables', {}),
                      'results': built.get('results', [])})
    info = _resolve_actual(matches)
    old = sheets.get_winners()
    new = {i: d for i, d in info.items() if not old.get(str(i))}
    won = {i: d['w'] for i, d in info.items() if d.get('w')}
    if won:
        sheets.set_winners_bulk(won)
        sheets.set_koinfo(info)
    import json as _j
    sheets._state_set('upcoming', _j.dumps(_upcoming_from(matches, info), ensure_ascii=False))
    try:
        merged = _auto_merge_names()
        if merged:
            logging.info('auto-merged identities: %s', merged)
    except Exception:
        logging.exception('auto-merge failed')
    return matches, new

async def _announce(ctx, new):
    """Instant result cards to the group for newly decided matches."""
    if not (GROUP_CHAT_ID and ANNOUNCE and new):
        return
    lines = ['⚡️ <b>Результат!</b>']
    for idx in sorted(new):
        lines.append(_result_line(idx, new[idx]))
        if idx >= SP_FROM_IDX:
            lines.extend(_sp_award_lines(idx, new[idx]))
    try:
        await ctx.bot.send_message(GROUP_CHAT_ID, '\n'.join(lines), parse_mode='HTML',
                                   disable_web_page_preview=True)
    except Exception:
        logging.exception('announce failed')

# ======================= scheduled jobs =======================
async def sync_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        _, new = await _do_sync(ctx)
        await _announce(ctx, new)
    except Exception:
        logging.exception('sync_job failed')

async def post_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).strftime('%d.%m')
    msg = _digest_text(f'🏟 <b>ЧМ-2026 · Сводка {today}</b>\n\n')
    await ctx.bot.send_message(GROUP_CHAT_ID, msg, parse_mode='HTML', disable_web_page_preview=True)
    _store_ranks(_leaderboard())

# ======================= admin =======================
async def sync_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text('Только для админа (проверь ADMIN_IDS).'); return
    if not (FD_TOKEN and results_api):
        await update.message.reply_text('⚠️ FOOTBALL_DATA_TOKEN не задан — авто-результаты выключены, используй /aw.'); return
    await update.message.reply_text('⏳ Тяну результаты…')
    try:
        matches, new = await _do_sync(ctx)
        won = sheets.get_winners()
        note = f'\n⚡ Новых результатов: {len(new)}' if new else ''
        merged = _auto_merge_names()
        if merged:
            note += '\n🔗 Объединил дубли: ' + '; '.join(merged)
        await update.message.reply_text(
            f'✅ Готово. Матчей в API: {len(matches)}. Решено матчей сетки: {len(won)}/31.{note}')
        await _announce(ctx, new)
    except Exception as e:
        await update.message.reply_text(f'⚠️ Ошибка: {type(e).__name__}: {str(e)[:200]}')

async def diag_cmd(update: Update, ctx):
    """Admin one-tap health check: what the API returns vs what resolved."""
    if not is_admin(update.effective_user.id):
        return
    if not (FD_TOKEN and results_api):
        await update.message.reply_text('FOOTBALL_DATA_TOKEN не задан.'); return
    try:
        loop = asyncio.get_event_loop()
        matches = await loop.run_in_executor(None, results_api.fetch_sync, FD_TOKEN)
    except Exception as e:
        await update.message.reply_text(f'API error: {type(e).__name__}: {str(e)[:150]}'); return
    ko = [m for m in matches if m.get('stage') and m['stage'] != 'GROUP_STAGE']
    fin = [m for m in ko if m.get('status') == 'FINISHED']
    info = _resolve_actual(matches)
    unmatched = []
    for m in fin:
        hit = any(pair_matches(m, d['home'], d['away']) for d in info.values())
        if not hit:
            unmatched.append(f"{m['stage']}: {m['home']}–{m['away']} {m['hs']}-{m['as']} ({m.get('duration')})")
    stages = sorted({m.get('stage') for m in ko if m.get('stage')})
    txt = (f'🔧 Диагностика\nВсего матчей API: {len(matches)} · KO: {len(ko)} · KO FINISHED: {len(fin)}\n'
           f'Стадии KO в API: {", ".join(stages) or "—"}\n'
           f'Замаплено на сетку: {len(info)}/31\n'
           f'Победителей в таблице: {len(sheets.get_winners())}/31')
    if unmatched:
        txt += '\n⚠️ FINISHED, но не замаплены:\n' + '\n'.join(unmatched[:6])
    await update.message.reply_text(txt)

async def post_cmd(update: Update, ctx):
    """Admin: /post — digest to the group; /post test — private preview (group untouched)."""
    if not is_admin(update.effective_user.id):
        return
    test = bool(ctx.args) and ctx.args[0].lower() == 'test'
    target = update.effective_chat.id if test else (GROUP_CHAT_ID or update.effective_chat.id)
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).strftime('%d.%m')
    head = '🧪 <b>ТЕСТ-ПРЕВЬЮ</b> (в группу не уходит)\n\n' if test else ''
    msg = _digest_text(head + f'🏟 <b>ЧМ-2026 · Сводка {today}</b>\n\n')
    await ctx.bot.send_message(target, msg, parse_mode='HTML', disable_web_page_preview=True)
    if not test:
        _store_ranks(_leaderboard())
        if GROUP_CHAT_ID:
            await update.message.reply_text('✅ Отправил в группу.')

async def aw_cmd(update: Update, ctx):   # /aw <match 1-31> <team>
    if not is_admin(update.effective_user.id):
        return
    try:
        m = int(ctx.args[0]); team = ' '.join(ctx.args[1:]); assert 1 <= m <= bracket.TOTAL and team
        idx = m - 1
        cands = [t for pair in bracket.R32_PAIRS for t in pair]
        for t in cands:
            if canon(t) == canon(team):
                team = t; break
        sheets.set_winner(idx, team)
        home, away = bracket.R32_PAIRS[idx] if idx < 16 else (None, None)
        sheets.set_koinfo({idx: {'w': team, 'home': home, 'away': away, 'hs': None, 'as': None,
                                 'date': _utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}})
        await update.message.reply_text(f'✅ Матч {m}: победитель {team}.')
    except Exception:
        await update.message.reply_text('Формат: /aw 1 Brazil  (номер матча 1–31)')

async def reset_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or ctx.args[0].lower() != 'confirm':
        n = len(sheets.all_submissions())
        await update.message.reply_text(
            f'⚠️ Это удалит ВСЕ прогнозы ({n} шт.), чтобы все заполнили заново. '
            'Подтверди: /reset confirm')
        return
    sheets.clear_submissions()
    await update.message.reply_text('✅ Все прогнозы обнулены. Попроси участников снова нажать /start.')

async def setdeadline_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        return
    sheets.set_deadline(' '.join(ctx.args))
    await update.message.reply_text(f'⏰ Дедлайн (по Алматы): {" ".join(ctx.args)}')

async def deadline_cmd(update: Update, ctx):
    d = sheets.get_deadline()
    await update.message.reply_text(f'⏰ Дедлайн (по Алматы): {d}' if d else 'Дедлайн не задан.')

async def facts_cmd(update: Update, ctx):
    facts = sheets.get_facts(); tb = facts.get('tables') or {}
    if not tb:
        await update.message.reply_text('Фактов пока нет — будут после /sync.'); return
    lines = ['📋 <b>Группы — места и очки</b>']
    for g in 'ABCDEFGHIJKL':
        rows = tb.get(g, [])
        if rows:
            lines.append(f"<b>{g}</b>: " + ' · '.join(f"{i+1}.{r['team']} {r['pts']}" for i, r in enumerate(rows)))
    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')

def ncanon(s):
    """Name normalizer (keeps latin + cyrillic letters)."""
    return re.sub(r'[^a-zа-яё]', '', (s or '').lower())

def _match_name(nm, names):
    """Find the single players-tab name that this telegram name refers to."""
    cn = ncanon(nm)
    if not cn:
        return None
    exact = [x for x in names if ncanon(x) == cn]
    if len(exact) == 1:
        return exact[0]
    tok = ncanon((nm or '').split()[0])
    if len(tok) >= 3:
        hits = [x for x in names
                if ncanon(x) == tok or ncanon(x).startswith(tok) or cn.startswith(ncanon(x))
                or any(ncanon(w) == tok for w in x.split())]
        hits = list(dict.fromkeys(hits))
        if len(hits) == 1:
            return hits[0]
    return None

def _auto_merge_names():
    """Auto-merge duplicated identities: score-game players whose telegram uid is not
    in the players tab get linked to their manually-imported row (matched by name).
    Runs on every sync; returns list of 'From → To' strings."""
    try:
        rows = sheets.player_rows_all()
    except Exception:
        rows = None
    if not rows:
        return []
    uid2name = {r[0]: r[1] for r in rows if r and len(r) >= 2}
    names = [r[1] for r in rows if r and len(r) >= 2 and r[1]]
    sp = _sp_all()
    idents = {}
    for preds in sp.values():
        for uid, p in preds.items():
            idents[uid] = p.get('n', '')
    merged, changed = [], False
    for uid, nm in idents.items():
        if uid in uid2name:                       # already linked: just align the name
            sheet_nm = uid2name[uid]
            if sheet_nm and sheet_nm != nm:
                for preds in sp.values():
                    if uid in preds and preds[uid].get('n') != sheet_nm:
                        preds[uid]['n'] = sheet_nm; changed = True
                merged.append(f'{nm} → {sheet_nm}')
            continue
        target = _match_name(nm, names)
        if not target:
            continue
        cur_uid = next((r[0] for r in rows if r and len(r) >= 2 and r[1] == target), '')
        if cur_uid and cur_uid.isdigit() and not cur_uid.startswith('9000'):
            continue                              # row belongs to another real account
        got = sheets.rebind_user_id(target, uid)
        if got:
            for preds in sp.values():
                if uid in preds:
                    preds[uid]['n'] = got; changed = True
            merged.append(f'{nm} → {got}')
    if changed:
        _set_json_state('sp', sp)
    return merged

async def iam_cmd(update: Update, ctx):
    """/iam <имя из таблицы> — привязать свой Telegram к строке, добавленной вручную.
    После привязки очки сетки и «Угадай счёт» считаются одному человеку."""
    name = ' '.join(ctx.args).strip()
    if not name:
        await update.message.reply_text('Формат: /iam Имя Фамилия (точно как в таблице лидеров)')
        return
    uid = update.effective_user.id
    sub = sheets.get_submission(uid)
    if sub:
        await update.message.reply_text(f'Ты уже привязан как «{sub["name"]}». Если нужно перепривязать — попроси админа: /link {uid} {name}')
        return
    matched = sheets.rebind_user_id(name, uid)
    if not matched:
        await update.message.reply_text(f'Не нашёл «{name}» в таблице. Проверь написание (см. /leaderboard).')
        return
    # merge score-game entries made under the telegram name into the sheet name
    sp = _sp_all(); changed = False
    for sidx, preds in sp.items():
        p = preds.get(str(uid))
        if p and p.get('n') != matched:
            p['n'] = matched; changed = True
    if changed:
        _set_json_state('sp', sp)
    await update.message.reply_text(f'✅ Готово! Теперь ты — «{matched}»: сетка и «Угадай счёт» считаются вместе.')

async def dupes_cmd(update: Update, ctx):
    """Admin: показать непривязанные аккаунты счёт-игры + готовые /link команды."""
    if not is_admin(update.effective_user.id):
        return
    rows = sheets.player_rows_all()
    uid2name = {r[0]: r[1] for r in rows if r and len(r) >= 2}
    names = [r[1] for r in rows if r and len(r) >= 2 and r[1]]
    sp = _sp_all()
    idents = {}
    for preds in sp.values():
        for uid, p in preds.items():
            idents[uid] = p.get('n', '')
    unlinked = {u: n for u, n in idents.items() if u not in uid2name}
    placeholders = [n for u, n in uid2name.items() if u.startswith('9000')]
    if not unlinked:
        txt = '✅ Непривязанных аккаунтов нет — дублей быть не должно.'
        if placeholders:
            txt += '\nЕщё без Telegram (норм, если они не тапают): ' + ', '.join(placeholders)
        await update.message.reply_text(txt)
        return
    lines = ['🔎 <b>Непривязанные аккаунты счёт-игры</b>',
             '<i>Скопируй нужную команду (поправь имя, если угадал не так):</i>', '']
    used = set()
    for uid, nm in unlinked.items():
        guess = _match_name(nm, names)
        if guess:
            used.add(guess)
    for uid, nm in unlinked.items():
        guess = _match_name(nm, names)
        free = [p for p in placeholders if p not in used]
        hint = guess or (free[0] if free else 'Имя_из_таблицы')
        if guess:
            lines.append(f'«{nm}» (id {uid}) ✅ похоже на <b>{guess}</b>')
        else:
            lines.append(f'«{nm}» (id {uid}) ❓ не распознал; свободны: {", ".join(free) or "—"}')
        lines.append(f'<code>/link {uid} {hint}</code>')
        lines.append('')
    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')

async def link_cmd(update: Update, ctx):
    """Admin: /link <uid> <имя> — привязать чужой Telegram id к строке таблицы."""
    if not is_admin(update.effective_user.id):
        return
    try:
        uid = int(ctx.args[0]); name = ' '.join(ctx.args[1:]).strip(); assert name
    except Exception:
        await update.message.reply_text('Формат: /link 123456789 Имя Фамилия'); return
    matched = sheets.rebind_user_id(name, uid)
    if not matched:
        await update.message.reply_text(f'Не нашёл «{name}» в таблице.'); return
    sp = _sp_all(); changed = False
    for sidx, preds in sp.items():
        p = preds.get(str(uid))
        if p and p.get('n') != matched:
            p['n'] = matched; changed = True
    if changed:
        _set_json_state('sp', sp)
    await update.message.reply_text(f'✅ {uid} ↔ «{matched}». Очки объединены.')

# ======================= FC26 (PS5) office cup =======================
def _fc_state():
    return _json_state('fc26', fc26.new_state())

def _fc_save(st):
    _set_json_state('fc26', st)

def _fc_is_player(st, uid, name):
    return any(p.get('uid') == uid or p['n'].lower() == (name or '').lower() for p in st['players'])

async def _fc_out(ctx, update, text):
    """FC announcements go to the GROUP; if the command came from a private chat —
    confirm there too."""
    target = GROUP_CHAT_ID or update.effective_chat.id
    await ctx.bot.send_message(target, text, parse_mode='HTML')
    if GROUP_CHAT_ID and str(update.effective_chat.id) != str(GROUP_CHAT_ID):
        try:
            await update.message.reply_text('✅ Отправил в группу.')
        except Exception:
            pass

async def fc_cmd(update: Update, ctx):
    """/fc — офисный турнир FC26. Подкоманды:
    join · add <имя> (админ) · start (админ) · <N> <X:Y> [победитель по пен.] ·
    round · table · next (админ) · help"""
    st = _fc_state()
    uid = update.effective_user.id
    args = ctx.args or []
    sub = args[0].lower() if args else 'help'

    if sub == 'join':
        name = ' '.join(args[1:]).strip() or update.effective_user.full_name
        if st['stage'] not in ('reg',):
            await update.message.reply_text('Регистрация закрыта — турнир уже идёт.'); return
        if _fc_is_player(st, uid, name):
            await update.message.reply_text('Ты уже в списке.'); return
        st['players'].append({'n': name, 'uid': uid}); _fc_save(st)
        await _fc_out(ctx, update, f'🎮 <b>{name}</b> в деле! Участников: <b>{len(st["players"])}</b>')
        return

    if sub == 'add':
        if not is_admin(uid): return
        name = ' '.join(args[1:]).strip()
        if not name:
            await update.message.reply_text('Формат: /fc add Имя'); return
        st['players'].append({'n': name, 'uid': None}); _fc_save(st)
        await update.message.reply_text(f'✅ Добавлен: {name}. Участников: {len(st["players"])}')
        return

    if sub == 'start':
        if not is_admin(uid): return
        if len(st['players']) < 4:
            await update.message.reply_text('Нужно минимум 4 участника.'); return
        st['stage'] = 'swiss'
        st['rounds'] = [fc26.pair_round(st)]; _fc_save(st)
        await _fc_out(ctx, update, '🏁 <b>Турнир FC26 стартовал!</b>\n\n' + fc26.fmt_round(st))
        return

    if sub == 'round':
        await update.message.reply_text(fc26.fmt_round(st), parse_mode='HTML'); return

    if sub == 'table':
        await update.message.reply_text(fc26.fmt_table(st), parse_mode='HTML'); return

    if sub == 'next':
        if not is_admin(uid): return
        await _fc_next(ctx, GROUP_CHAT_ID or update.effective_chat.id, st)
        if GROUP_CHAT_ID and str(update.effective_chat.id) != str(GROUP_CHAT_ID):
            await update.message.reply_text('✅ Отправил в группу.')
        return

    if sub.isdigit():          # /fc <N> <X:Y> [имя победителя по пенальти]
        if not (is_admin(uid) or _fc_is_player(st, uid, update.effective_user.full_name)):
            await update.message.reply_text('Счёт могут вносить участники турнира и админ.'); return
        try:
            n = int(sub); score = args[1]
            ga, gb = (int(x) for x in score.replace('-', ':').split(':'))
            pen = ' '.join(args[2:]).strip() or None
        except Exception:
            await update.message.reply_text('Формат: /fc 1 2:1   (в плей-офф при ничьей: /fc 1 2:2 Имя)')
            return
        ok, msg = fc26.report(st, n, ga, gb, pen)
        if ok:
            _fc_save(st)
            out = f'✅ {msg}'
            if st['stage'] == 'swiss' and fc26.round_done(st['rounds'][-1]):
                out += '\n\n🏁 Тур доигран! ' + ('Следующий: /fc next' if len(st['rounds']) < fc26.SWISS_ROUNDS
                                                 else 'Дальше плей-офф: /fc next')
                out += '\n\n' + fc26.fmt_table(st)
            elif st['stage'] == 'semis' and fc26.semis_done(st):
                out += '\n\n🏁 Полуфиналы сыграны! Финал: /fc next'
            elif st['stage'] == 'final' and fc26.final_done(st):
                out += '\n\n🏆 Финал сыгран! Итоги: /fc next'
            await _fc_out(ctx, update, out)
        else:
            await update.message.reply_text('⚠️ ' + msg)
        return

    # default: friendly button menu
    kb = [[InlineKeyboardButton('🎮 Я участвую!', callback_data='fc:join')],
          [InlineKeyboardButton('📋 Кто с кем играет', callback_data='fc:round'),
           InlineKeyboardButton('🏆 Таблица', callback_data='fc:table')]]
    if is_admin(uid):
        kb.append([InlineKeyboardButton('▶️ Старт турнира', callback_data='fc:start'),
                   InlineKeyboardButton('⏭ Следующий этап', callback_data='fc:next')])
    n = len(st['players'])
    await update.message.reply_text(
        '🎮 <b>FC26 · офисный кубок (PS5)</b>\n'
        f'Участников: <b>{n}</b> · 3 тура швейцарки → полуфиналы → финал\n'
        'Победа 3 · ничья 1 · отдых (bye) +3\n'
        '⏰ Матчи: 13:00–14:00 и 18:00–19:00 · финал чт в обед\n\n'
        '<i>Счёт после матча — одной строкой: /fc 1 2:1\n'
        '(номер матча — в «Кто с кем играет»)</i>',
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

async def _on_fc_tap(update: Update, ctx):
    q = update.callback_query
    action = q.data.split(':', 1)[1]
    st = _fc_state()
    uid = q.from_user.id
    if action == 'join':
        name = q.from_user.full_name
        if st['stage'] != 'reg':
            await q.answer('Регистрация закрыта — турнир уже идёт.', show_alert=True); return
        if _fc_is_player(st, uid, name):
            await q.answer('Ты уже в списке! 👌'); return
        st['players'].append({'n': name, 'uid': uid}); _fc_save(st)
        await q.answer(f'🎮 Ты в игре, {name}!')
        try:
            await ctx.bot.send_message(GROUP_CHAT_ID or q.message.chat_id,
                                       f'🎮 <b>{name}</b> в деле! Участников: <b>{len(st["players"])}</b>\n'
                                       + ' · '.join(p['n'] for p in st['players']), parse_mode='HTML')
        except Exception:
            pass
        return
    if action == 'round':
        await q.answer()
        await ctx.bot.send_message(q.message.chat_id, fc26.fmt_round(st), parse_mode='HTML')
        return
    if action == 'table':
        await q.answer()
        await ctx.bot.send_message(q.message.chat_id, fc26.fmt_table(st), parse_mode='HTML')
        return
    if action in ('start', 'next'):
        if not is_admin(uid):
            await q.answer('Только для админа.', show_alert=True); return
        await q.answer()
        target = GROUP_CHAT_ID or q.message.chat_id
        if action == 'start':
            if len(st['players']) < 4:
                await ctx.bot.send_message(q.message.chat_id, 'Нужно минимум 4 участника.'); return
            if st['stage'] != 'reg':
                await ctx.bot.send_message(q.message.chat_id, 'Турнир уже запущен.'); return
            st['stage'] = 'swiss'; st['rounds'] = [fc26.pair_round(st)]; _fc_save(st)
            await ctx.bot.send_message(target, '🏁 <b>Турнир FC26 стартовал!</b>\n\n' + fc26.fmt_round(st),
                                       parse_mode='HTML')
        else:
            await _fc_next(ctx, target, st)

async def _fc_next(ctx, chat_id, st):
    if st['stage'] == 'swiss':
        if not fc26.round_done(st['rounds'][-1]):
            await ctx.bot.send_message(chat_id, 'Ещё не все счета тура внесены.'); return
        if len(st['rounds']) >= fc26.SWISS_ROUNDS:
            top = fc26.start_semis(st); _fc_save(st)
            await ctx.bot.send_message(chat_id, '🏁 Швейцарка окончена! Топ-4: ' + ', '.join(top) +
                                       '\n\n' + fc26.fmt_round(st), parse_mode='HTML')
        else:
            st['rounds'].append(fc26.pair_round(st)); _fc_save(st)
            await ctx.bot.send_message(chat_id, fc26.fmt_round(st), parse_mode='HTML')
    elif st['stage'] == 'semis':
        if not fc26.semis_done(st):
            await ctx.bot.send_message(chat_id, 'Полуфиналы ещё не доиграны.'); return
        fc26.start_final(st); _fc_save(st)
        await ctx.bot.send_message(chat_id, fc26.fmt_round(st), parse_mode='HTML')
    elif st['stage'] == 'final':
        if not fc26.final_done(st):
            await ctx.bot.send_message(chat_id, 'Финал ещё не сыгран.'); return
        st['stage'] = 'done'; _fc_save(st)
        await ctx.bot.send_message(chat_id,
            f'🏆 <b>ЧЕМПИОН FC26: {fc26.champion(st)}</b>\n🥉 Бронза: {fc26.bronze_winner(st)}\n\n'
            + fc26.fmt_table(st), parse_mode='HTML')
    else:
        await ctx.bot.send_message(chat_id, 'Турнир не активен.')

async def fc_remind_job(ctx: ContextTypes.DEFAULT_TYPE):
    """15 minutes before each slot: who plays whom (only unplayed matches)."""
    if not GROUP_CHAT_ID:
        return
    st = _fc_state()
    if st['stage'] not in ('swiss', 'semis', 'final'):
        return
    label, block = fc26.current_block(st)
    if not block:
        return
    todo = [(i + 1, a, b) for i, (a, b) in enumerate(block['pairs'])
            if not block['res'].get(str(i))]
    if not todo:
        return
    lines = [f'⏰ <b>FC26 · через 15 минут слот!</b> ({label})']
    for n, a, b in todo:
        lines.append(f'🎮 {a} 🆚 {b}   <i>(счёт: /fc {n} X:Y)</i>')
    try:
        await ctx.bot.send_message(GROUP_CHAT_ID, '\n'.join(lines), parse_mode='HTML')
    except Exception:
        logging.exception('fc reminder failed')

async def id_cmd(update: Update, ctx):
    uid = update.effective_user.id
    await update.message.reply_text(f'Твой id: {uid}\n' + ('✅ ты админ' if is_admin(uid) else '❗ впиши это число в ADMIN_IDS'))

async def chatid_cmd(update: Update, ctx):
    c = update.effective_chat
    note = ('\n👉 id ГРУППЫ — впиши в GROUP_CHAT_ID.' if c.type in ('group', 'supergroup')
            else '\n(Это личный чат. Напиши /chatid в группе, чтобы узнать её id.)')
    await update.message.reply_text(f'id чата: {c.id} ({c.type}){note}')

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    for cmd, fn in [('start', start), ('restart', restart), ('mybracket', mybracket),
                    ('leaderboard', leaderboard_cmd), ('facts', facts_cmd), ('sync', sync_cmd),
                    ('post', post_cmd), ('aw', aw_cmd), ('reset', reset_cmd), ('setdeadline', setdeadline_cmd),
                    ('deadline', deadline_cmd), ('id', id_cmd), ('chatid', chatid_cmd), ('diag', diag_cmd),
                    ('spost', spost_cmd), ('iam', iam_cmd), ('link', link_cmd), ('fc', fc_cmd),
                    ('dupes', dupes_cmd)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_callback))

    async def on_error(update, ctx):
        logging.error('Update error: %s', ctx.error)
    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_repeating(sync_job, interval=SYNC_EVERY_H * 3600, first=30)
        app.job_queue.run_daily(post_job, time=dt.time(hour=(POST_HOUR - TZ_OFFSET) % 24, minute=0))
        app.job_queue.run_daily(sp_post_job, time=dt.time(hour=(SP_POST_HOUR - TZ_OFFSET) % 24, minute=0))
        for hh, mm in ((12, 45), (17, 45)):        # FC26: 15 min before each play slot (Almaty)
            app.job_queue.run_daily(fc_remind_job, time=dt.time(hour=(hh - TZ_OFFSET) % 24, minute=mm))
        print(f'Scheduled: sync every {SYNC_EVERY_H}h / post {POST_HOUR}:00 / score-cards {SP_POST_HOUR}:00 '
              f'/ FC26 reminders 12:45 & 17:45 Almaty.')
    else:
        print('WARNING: job_queue is None. Add python-telegram-bot[job-queue] to requirements.')
    print('Bot running…')
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
