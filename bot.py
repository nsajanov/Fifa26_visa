# -*- coding: utf-8 -*-
"""WC 2026 PLAYOFF predictor bot — fully automatic.
Group stage = facts only (viewed via /facts). The prediction game is the knockout:
everyone predicts the same real R32 bracket, then earns points. Once a day the bot
pulls real results from football-data.org, recomputes, and posts the leaderboard."""
import os, json, asyncio, datetime as dt
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import sheets, scoring, results_api
from fixtures import ROUND_OF, GROUPS

BOT_TOKEN = os.environ['BOT_TOKEN']
WEBAPP_URL = os.getenv('WEBAPP_URL', '')
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x.strip().isdigit()}
GROUP_CHAT_ID = os.getenv('GROUP_CHAT_ID', '')
FD_TOKEN = os.getenv('FOOTBALL_DATA_TOKEN', '')
SYNC_HOUR = int(os.getenv('SYNC_HOUR', '10'))   # auto-sync hour, Almaty time
POST_HOUR = int(os.getenv('POST_HOUR', '11'))   # auto-post hour, Almaty time
TZ_OFFSET = int(os.getenv('TZ_OFFSET', '5'))    # Almaty = UTC+5 (no daylight saving)

def is_admin(uid):
    return uid in ADMIN_IDS

def _deadline_passed():
    d = sheets.get_deadline()
    if not d:
        return False
    try:
        now_almaty = dt.datetime.utcnow() + dt.timedelta(hours=TZ_OFFSET)
        return now_almaty > dt.datetime.strptime(d, '%Y-%m-%d %H:%M')
    except ValueError:
        return False

# ---------- players ----------
async def start(update: Update, ctx):
    kb = [[KeyboardButton('📝 Прогноз плей-офф', web_app=WebAppInfo(url=WEBAPP_URL))]] if WEBAPP_URL else []
    await update.message.reply_text(
        'Привет! Это предиктор плей-офф Чемпионата мира 2026 ⚽\n\n'
        'Групповой этап — просто факты (смотри /facts). Игра начинается с плей-офф: '
        'все прогнозируют одну и ту же реальную сетку Round of 32 и набирают очки.\n\n'
        'Жми «Прогноз плей-офф», поставь счёт и победителя каждого матча до чемпиона.\n\n'
        '💡 Не знаешь, кого ставить? Напиши /facts — увидишь, кто лидирует в группах прямо сейчас, '
        'и /facts A — со счётом каждого матча. Это твоя подсказка.\n\n'
        '/facts — результаты и таблицы групп · /me — мои очки · /leaderboard · /deadline',
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True) if kb else None)

async def help_cmd(update: Update, ctx):
    txt = ('/facts — факты матчей и таблицы групп\n/me — мои очки\n/leaderboard — таблица\n/deadline')
    if is_admin(update.effective_user.id):
        txt += ('\n\nАдмин (обычно не нужно — всё авто):\n/sync — подтянуть результаты сейчас\n'
                '/win 73 Brazil — поправить победителя матча\n/setdeadline 2026-06-28 16:00\n/post')
    await update.message.reply_text(txt)

async def on_webapp(update: Update, ctx):
    uid = update.effective_user.id
    if _deadline_passed():
        await update.message.reply_text('⛔ Приём прогнозов закрыт — дедлайн прошёл.')
        return
    try:
        data = json.loads(update.message.web_app_data.data)
    except Exception:
        await update.message.reply_text('Не смог прочитать прогноз, попробуй ещё раз.')
        return
    name = data.get('name') or update.effective_user.full_name
    sheets.save_submission(uid, name, {'ko': data.get('ko', {})})
    n = len(data.get('ko', {})); champ = data.get('ko', {}).get('104', {}).get('w', '—')
    await update.message.reply_text(
        f'✅ Прогноз сохранён, {name}!\nМатчей заполнено: {n}/32 · чемпион: {champ}\n'
        'Можно вернуться и поправить до дедлайна.')

async def me(update: Update, ctx):
    sub = sheets.get_submission(update.effective_user.id)
    if not sub:
        await update.message.reply_text('Ты ещё не заполнил прогноз. /start → «Прогноз плей-офф».')
        return
    res = scoring.score_submission(sub['submission'], sheets.get_actual())
    lb = scoring.leaderboard(sheets.all_submissions(), sheets.get_actual())
    rank = next((i + 1 for i, (n, _) in enumerate(lb) if n == sub['name']), '—')
    await update.message.reply_text(
        f"🎯 {sub['name']} — {res['total']} очков (место {rank})\n"
        f"За победителей: {res['win_pts']} · за счёт: {res['bonus']}\n"
        f"Угадано матчей: {res['correct']}/32 · точных счетов: {res['exact']}")

def _fmt_lb(lb, me_name=None, top=15):
    medals = {1: '🥇', 2: '🥈', 3: '🥉'}
    out = ['🏆 Таблица лидеров']
    for i, (name, sc) in enumerate(lb[:top], 1):
        tag = medals.get(i, f'{i}.')
        out.append(f"{tag} {name} — {sc['total']}{' ◄ ты' if name == me_name else ''}")
    return '\n'.join(out) if len(out) > 1 else '🏆 Пока нет прогнозов.'

async def leaderboard_cmd(update: Update, ctx):
    sub = sheets.get_submission(update.effective_user.id)
    lb = scoring.leaderboard(sheets.all_submissions(), sheets.get_actual())
    await update.message.reply_text(_fmt_lb(lb, sub['name'] if sub else None))

def _overview_text():
    facts = sheets.get_facts(); tb = facts.get('tables') or {}
    if not tb:
        return None
    lines = ['📋 <b>Группы — места и очки</b> (подсказка, кого ставить в плей-офф).',
             'Полные счета по группе: напиши <b>/facts A</b> (B, C …)\n']
    for g in 'ABCDEFGHIJKL':
        rows = tb.get(g, [])
        if rows:
            lines.append(f"<b>{g}</b>: " + ' · '.join(f"{i+1}.{r['team']} {r['pts']}" for i, r in enumerate(rows)))
    champ = sheets.get_actual().get('ko', {}).get('104', {}).get('w')
    if champ:
        lines.append(f'\n🏆 Чемпион: {champ}')
    return '\n'.join(lines)

def _group_detail(g):
    facts = sheets.get_facts(); rows = (facts.get('tables') or {}).get(g, [])
    if not rows:
        return None
    out = [f'<b>Группа {g}</b>']
    for i, r in enumerate(rows, 1):
        mark = ' ✅' if i <= 2 else (' (3-е)' if i == 3 else '')
        out.append(f"{i}. {r['team']} — {r['pts']} очк · {r['gf']}:{r['ga']} ({r['gd']:+d}){mark}")
    res = [x for x in (facts.get('results') or []) if x.get('group') == g]
    if res:
        out.append('Матчи:')
        for x in res:
            out.append(f"  {x['home']} {x['hs']}:{x['as']} {x['away']}")
    return '\n'.join(out)

async def facts_cmd(update: Update, ctx):
    tb = sheets.get_facts().get('tables') or {}
    if not tb:
        await update.message.reply_text('Фактов пока нет — появятся после /sync, когда сыграют первые матчи.')
        return
    arg = (ctx.args[0].upper() if ctx.args else '')
    if arg in 'ABCDEFGHIJKL' and arg:
        await update.message.reply_text(_group_detail(arg) or 'Нет данных по группе.', parse_mode='HTML')
        return
    await update.message.reply_text(_overview_text(), parse_mode='HTML')

async def results_cmd(update: Update, ctx):
    tb = sheets.get_facts().get('tables') or {}
    if not tb:
        await update.message.reply_text('Результатов пока нет — нажми /sync (или подожди утренней сводки).')
        return
    for half in ('ABCDEF', 'GHIJKL'):
        block = '\n\n'.join(x for x in (_group_detail(g) for g in half) if x)
        if block:
            await update.message.reply_text(block, parse_mode='HTML')

async def deadline_cmd(update: Update, ctx):
    d = sheets.get_deadline()
    await update.message.reply_text(f'⏰ Дедлайн (по Алматы): {d}' if d else 'Дедлайн не задан.')

async def id_cmd(update: Update, ctx):
    uid = update.effective_user.id
    ok = '✅ ты уже админ' if is_admin(uid) else '❗ ты пока НЕ админ — впиши это число в ADMIN_IDS на Railway'
    await update.message.reply_text(f'Твой Telegram id: {uid}\n{ok}')

async def chatid_cmd(update: Update, ctx):
    c = update.effective_chat
    note = ('\n👉 Это id ГРУППЫ — впиши его в GROUP_CHAT_ID на Railway.'
            if c.type in ('group', 'supergroup')
            else '\n(Это личный чат. Чтобы узнать id ГРУППЫ — добавь бота в группу и напиши там /chatid.)')
    await update.message.reply_text(f'id этого чата: {c.id}\nтип: {c.type}{note}')

# ---------- the automatic core ----------
async def _do_sync(ctx):
    """Pull real results, rebuild the bracket, recompute, return (actual, changed)."""
    if not FD_TOKEN:
        return None
    loop = asyncio.get_event_loop()
    matches = await loop.run_in_executor(None, results_api.fetch_sync, FD_TOKEN)
    built = results_api.build_actual(matches)
    sheets.set_actual(built)
    sheets.set_facts({'standings': built['standings']})
    return built

def _broadcast_messages(bot_username=None):
    """Returns a list of messages: (1) leader + full leaderboard + links,
    (2) full results groups A–F, (3) groups G–L. Split to fit Telegram limits."""
    facts = sheets.get_facts(); tb = facts.get('tables') or {}
    actual = sheets.get_actual()
    today = dt.date.today().strftime('%d.%m.%Y')
    lb = scoring.leaderboard(sheets.all_submissions(), actual)
    head = [f'☀️ <b>Сводка на {today}</b>']
    if any(s['total'] > 0 for _, s in lb):
        leader = lb[0]; sniper = max(lb, key=lambda r: r[1]['exact'])
        head.append(f"🏆 Лидер: <b>{leader[0]}</b> — {leader[1]['total']} очк.")
        if sniper[1]['exact'] > 0:
            head.append(f"🎯 Точнее всех по счёту: {sniper[0]} ({sniper[1]['exact']})")
    else:
        head.append('🎮 Очки пойдут с плей-офф. Группы сейчас — подсказка.')
    head.append('\n' + _fmt_lb(lb))          # кто сколько набрал
    champ = actual.get('ko', {}).get('104', {}).get('w')
    if champ:
        head.append(f'\n🏆 Чемпион: {champ}')
    head.append('\n💡 Все счета — /results · по группе — /facts A · мой счёт — /me')
    if bot_username:
        head.append(f'🔗 Бот и прогноз: https://t.me/{bot_username}')
    msgs = ['\n'.join(head)]
    if tb:
        for half, label in (('ABCDEF', 'A–F'), ('GHIJKL', 'G–L')):
            block = '\n\n'.join(x for x in (_group_detail(g) for g in half) if x)
            if block:
                msgs.append(f'📋 <b>Результаты · группы {label}</b>\n\n' + block)
    return msgs

async def sync_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Silent auto-refresh of results (no posting) — keeps /facts & /results fresh."""
    try:
        await _do_sync(ctx)
    except Exception:
        pass

async def post_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Auto-post the results + leaderboard to the group (no fetch here)."""
    if not GROUP_CHAT_ID:
        return
    for m in _broadcast_messages(ctx.bot.username):
        await ctx.bot.send_message(chat_id=GROUP_CHAT_ID, text=m, parse_mode='HTML',
                                   disable_web_page_preview=True)

# ---------- admin (rarely needed) ----------
async def sync_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text('Эта команда только для админа. Проверь переменную ADMIN_IDS.')
        return
    if not FD_TOKEN:
        await update.message.reply_text('⚠️ Не задан FOOTBALL_DATA_TOKEN в переменных Railway — автоподтяг выключен.')
        return
    await update.message.reply_text('⏳ Тяну результаты из football-data.org…')
    try:
        loop = asyncio.get_event_loop()
        matches = await loop.run_in_executor(None, results_api.fetch_sync, FD_TOKEN)
        built = results_api.build_actual(matches)
        sheets.set_actual(built)
        sheets.set_facts({'standings': built['standings'], 'tables': built.get('tables', {}),
                          'results': built.get('results', [])})
        await update.message.reply_text(
            f'✅ Готово.\nЗавершённых матчей в API: {len(matches)}\n'
            f'Группы сыграны полностью: {"да" if built["groups_done"] else "ещё нет"}\n'
            f'Матчей плей-офф разобрано: {len(built["ko"])}/32\n\n'
            f'Теперь проверь /facts. Если матчей 0 — значит на этом ключе нет данных ЧМ '
            f'(нужен другой источник, напиши мне).')
    except Exception as e:
        await update.message.reply_text(
            f'⚠️ Не получилось подтянуть.\nОшибка: {type(e).__name__}: {str(e)[:300]}\n\n'
            f'Если тут «403/Forbidden» — ключ неверный или тариф не отдаёт ЧМ. '
            f'Если «404/Not Found» — нет данных по турниру. Пришли мне этот текст.')

async def bracket_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text('Только для админа.')
        return
    ko = sheets.get_actual().get('ko', {})
    seed = {str(m): {'home': ko[str(m)]['home'], 'away': ko[str(m)]['away']}
            for m in range(73, 89)
            if str(m) in ko and ko[str(m)].get('home') and ko[str(m)].get('away')}
    if len(seed) < 16:
        await update.message.reply_text(
            f'Реальная сетка R32 ещё не готова ({len(seed)}/16). Сделай /sync после жеребьёвки '
            f'плей-офф (когда сыграны все группы) — и повтори /bracket.')
        return
    await update.message.reply_text(
        'Готово! Скопируй текст ниже и сохрани его как файл bracket.json РЯДОМ с index.html '
        '(в том же репозитории/Netlify). После этого мини-игра у ВСЕХ откроется с реальными '
        '32 командами — равный старт.\n\n' + json.dumps(seed, ensure_ascii=False))

async def win_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        return
    try:
        m = int(ctx.args[0]); team = ' '.join(ctx.args[1:]); assert m in ROUND_OF and team
        sheets.set_ko_winner(m, team)
        await update.message.reply_text(f'✅ M{m} победитель поправлен: {team}.')
    except Exception:
        await update.message.reply_text('Формат: /win 73 Brazil')

async def setdeadline_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        return
    val = ' '.join(ctx.args); sheets.set_deadline(val)
    await update.message.reply_text(f'⏰ Дедлайн: {val}')

async def post_cmd(update: Update, ctx):
    if not is_admin(update.effective_user.id):
        return
    target = GROUP_CHAT_ID or update.effective_chat.id
    if not GROUP_CHAT_ID:
        await update.message.reply_text(
            '⚠️ GROUP_CHAT_ID не задан на Railway — отправляю сюда. Чтобы слать в группу, '
            'добавь бота в группу, напиши там /chatid и впиши число в GROUP_CHAT_ID.')
    try:
        for m in _broadcast_messages(ctx.bot.username):
            await ctx.bot.send_message(chat_id=target, text=m, parse_mode='HTML',
                                       disable_web_page_preview=True)
        if GROUP_CHAT_ID:
            await update.message.reply_text(f'✅ Отправил сводку в группу (id {GROUP_CHAT_ID}). Проверь её.')
    except Exception as e:
        await update.message.reply_text(
            f'⚠️ Не смог отправить в группу (id {GROUP_CHAT_ID}).\nОшибка: {type(e).__name__}: {str(e)[:200]}\n'
            'Проверь: бот добавлен в эту группу? id правильный (с минусом)?')

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    for cmd, fn in [('start', start), ('help', help_cmd), ('me', me),
                    ('leaderboard', leaderboard_cmd), ('facts', facts_cmd),
                    ('deadline', deadline_cmd), ('id', id_cmd), ('chatid', chatid_cmd),
                    ('results', results_cmd), ('sync', sync_cmd),
                    ('bracket', bracket_cmd), ('win', win_cmd),
                    ('setdeadline', setdeadline_cmd), ('post', post_cmd)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp))

    async def on_error(update, ctx):
        import logging
        logging.error('Update error: %s', ctx.error)
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

