# -*- coding: utf-8 -*-
"""WC 2026 Playoff Predictor — in-chat buttons + automatic results.
Players predict by tapping who advances, right in the chat (no mini-app).
The bot auto-pulls real results (football-data.org) at SYNC_HOUR and posts the
results + leaderboard to the group at POST_HOUR (Almaty time)."""
import os, asyncio, datetime as dt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, ContextTypes)

import sheets, bracket
try:
    import results_api
except Exception:
    results_api = None

BOT_TOKEN = os.environ['BOT_TOKEN']
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x.strip().isdigit()}
GROUP_CHAT_ID = os.getenv('GROUP_CHAT_ID', '')
FD_TOKEN = os.getenv('FOOTBALL_DATA_TOKEN', '')
SYNC_HOUR = int(os.getenv('SYNC_HOUR', '10'))
POST_HOUR = int(os.getenv('POST_HOUR', '11'))
TZ_OFFSET = int(os.getenv('TZ_OFFSET', '5'))      # Almaty UTC+5

def is_admin(uid):
    return uid in ADMIN_IDS

def _deadline_passed():
    d = sheets.get_deadline()
    if not d:
        return False
    try:
        return dt.datetime.utcnow() + dt.timedelta(hours=TZ_OFFSET) > dt.datetime.strptime(d, '%Y-%m-%d %H:%M')
    except ValueError:
        return False

# ======================= prediction flow (buttons) =======================
async def start(update: Update, ctx):
    # одно нажатие START -> сразу первый матч, без лишних кнопок
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
    lines = ['📋 Твой прогноз:']
    for r, nm in enumerate(bracket.ROUND_NAMES):
        ws = [picks[i] for i in range(bracket.OFFSETS[r], bracket.OFFSETS[r] + bracket.ROUND_SIZES[r]) if i in picks]
        if ws:
            lines.append(f'<b>{nm}</b>: ' + ', '.join(ws))
    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')

# ======================= scoring / leaderboard =======================
def _score(picks, actual=None):
    if actual is None:
        actual = sheets.get_winners()
    total = 0.0; correct = 0
    for k, team in picks.items():
        i = int(k)
        if actual.get(str(i)) and actual[str(i)] == team:
            total += bracket.ROUND_POINTS[bracket.round_of(i)]; correct += 1
    return total, correct

def _leaderboard():
    actual = sheets.get_winners()                      # fetch ONCE, not per player
    subs = sheets.all_submissions()                    # cached read
    rows = [(name, *_score(sub.get('picks', {}), actual)) for name, sub in subs.items()]
    rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
    return rows

def _fmt_lb(rows, top=20):
    medals = {1: '🥇', 2: '🥈', 3: '🥉'}
    out = ['🏆 Таблица лидеров']
    for i, (name, t, c) in enumerate(rows[:top], 1):
        out.append(f"{medals.get(i, str(i)+'.')} {name} — {t:g} ({c})")
    return '\n'.join(out) if len(out) > 1 else '🏆 Пока нет прогнозов.'

async def leaderboard_cmd(update: Update, ctx):
    await update.message.reply_text(_fmt_lb(_leaderboard()))

# ======================= results / facts =======================
def _facts_text():
    facts = sheets.get_facts(); tb = facts.get('tables') or {}
    if not tb:
        return None
    lines = ['📋 <b>Группы — места и очки</b>']
    for g in 'ABCDEFGHIJKL':
        rows = tb.get(g, [])
        if rows:
            lines.append(f"<b>{g}</b>: " + ' · '.join(f"{i+1}.{r['team']} {r['pts']}" for i, r in enumerate(rows)))
    return '\n'.join(lines)

async def facts_cmd(update: Update, ctx):
    t = _facts_text()
    await update.message.reply_text(t or 'Фактов пока нет — будут после /sync.', parse_mode='HTML')

def _norm(s):
    return (s or '').strip().lower()

def _resolve_actual(matches):
    """Map real finished matches onto the 31 bracket positions -> {idx: winner}."""
    fin = [m for m in matches if m.get('hs') is not None and m.get('as') is not None]
    def winner_of(m):
        if m['hs'] > m['as']: return m['home']
        if m['as'] > m['hs']: return m['away']
        return None
    def find(t0, t1):
        s = {_norm(t0), _norm(t1)}
        for m in fin:
            if {_norm(m['home']), _norm(m['away'])} == s:
                return m
        return None
    actual = {}
    for idx in range(bracket.TOTAL):
        if idx < 16:
            t0, t1 = bracket.R32_PAIRS[idx]
        else:
            f0, f1 = bracket.feeders(idx)
            t0, t1 = actual.get(f0), actual.get(f1)
        if not t0 or not t1:
            continue
        m = find(t0, t1)
        if m:
            w = winner_of(m)
            if w:
                actual[idx] = t0 if _norm(w) == _norm(t0) else t1
    return actual

async def _do_sync(ctx):
    if not (FD_TOKEN and results_api):
        return None
    loop = asyncio.get_event_loop()
    matches = await loop.run_in_executor(None, results_api.fetch_sync, FD_TOKEN)
    built = results_api.build_actual(matches)
    sheets.set_facts({'standings': built.get('standings', {}), 'tables': built.get('tables', {}),
                      'results': built.get('results', [])})
    won = _resolve_actual(matches)
    if won:
        sheets.set_winners_bulk(won)
    return matches

# ======================= scheduled jobs =======================
async def sync_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await _do_sync(ctx)
    except Exception:
        pass

async def post_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    today = dt.date.today().strftime('%d.%m')
    msg = f'☀️ Сводка {today}\n\n' + _fmt_lb(_leaderboard())
    facts = _facts_text()
    if facts:
        msg += '\n\n' + facts
    await ctx.bot.send_message(GROUP_CHAT_ID, msg, parse_mode='HTML', disable_web_page_preview=True)

# ======================= admin =======================
async def sync_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text('Только для админа (проверь ADMIN_IDS).'); return
    if not (FD_TOKEN and results_api):
        await update.message.reply_text('⚠️ FOOTBALL_DATA_TOKEN не задан — авто-результаты выключены, используй /aw.'); return
    await update.message.reply_text('⏳ Тяну результаты…')
    try:
        matches = await _do_sync(ctx)
        won = sheets.get_winners()
        await update.message.reply_text(f'✅ Готово. Матчей в API: {len(matches)}. Решено матчей сетки: {len(won)}/31.')
    except Exception as e:
        await update.message.reply_text(f'⚠️ Ошибка: {type(e).__name__}: {str(e)[:200]}')

async def post_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        return
    target = GROUP_CHAT_ID or update.effective_chat.id
    today = dt.date.today().strftime('%d.%m')
    msg = f'☀️ Сводка {today}\n\n' + _fmt_lb(_leaderboard())
    facts = _facts_text()
    if facts:
        msg += '\n\n' + facts
    await ctx.bot.send_message(target, msg, parse_mode='HTML', disable_web_page_preview=True)
    if GROUP_CHAT_ID:
        await update.message.reply_text('✅ Отправил в группу.')

async def aw_cmd(update: Update, ctx):   # /aw <match 1-31> <team>
    if not is_admin(update.effective_user.id):
        return
    try:
        m = int(ctx.args[0]); team = ' '.join(ctx.args[1:]); assert 1 <= m <= bracket.TOTAL and team
        sheets.set_winner(m - 1, team)
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
                    ('deadline', deadline_cmd), ('id', id_cmd), ('chatid', chatid_cmd)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_callback))

    async def on_error(update, ctx):
        import logging; logging.error('Update error: %s', ctx.error)
    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_daily(sync_job, time=dt.time(hour=(SYNC_HOUR - TZ_OFFSET) % 24, minute=0))
        app.job_queue.run_daily(post_job, time=dt.time(hour=(POST_HOUR - TZ_OFFSET) % 24, minute=0))
        print(f'Scheduled: sync {SYNC_HOUR}:00 / post {POST_HOUR}:00 Almaty.')
    else:
        print('WARNING: job_queue is None. Add python-telegram-bot[job-queue] to requirements.')
    print('Bot running…')
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
