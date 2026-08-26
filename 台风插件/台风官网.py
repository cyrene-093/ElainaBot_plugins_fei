__plugin_meta__ = {
    'name': '台风官网图',
    'author': '茉莉奶绿（原创） / 飞行漂绒（修改优化）',
    'description': '中央气象台台风查询（文字+官网路径产品图）',
    'version': '1.0.0',
}

import asyncio
import json
import os
import re
import sqlite3
import time
import traceback
from datetime import datetime
from urllib.parse import quote

import aiohttp
from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_unload

log = get_logger(PLUGIN, '台风官网')

NMC = 'https://typhoon.nmc.cn/weatherservice/typhoon/jsons'
NMC_PUB = 'https://www.nmc.cn/publish/typhoon'
NMC_IMG = 'https://image.nmc.cn'
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'taifeng_official.db')

_LEVEL_CN = {
    'TD': '热带低压', 'TS': '热带风暴', 'STS': '强热带风暴',
    'TY': '台风', 'STY': '强台风', 'SuperTY': '超强台风',
}
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
    'Referer': 'https://typhoon.nmc.cn/web.html',
    'Accept': '*/*',
}
_PUB_HEADERS = {
    **_HEADERS,
    'Referer': 'https://www.nmc.cn/publish/typhoon/probability.html',
}

# 官网路径产品页（中央台按活跃台风分页面发布）
_PUB_PAGES = (
    'probability.html',
    'probability-img2.html',
    'probability-img3.html',
    'probability-img4.html',
    'probability-img5.html',
    'probability-img6.html',
    'probability-img7.html',
    'probability-img8.html',
)

# 复用会话 / 短缓存
_SESSION = None
_SESSION_LOCK = asyncio.Lock()
_MEM_CACHE = {}
_LIST_TTL, _VIEW_TTL, _OFFICIAL_TTL, _IMG_TTL = 60, 90, 180, 300


def _cache_get(key):
    item = _MEM_CACHE.get(key)
    if not item:
        return None
    exp, val = item
    if time.time() > exp:
        _MEM_CACHE.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl):
    _MEM_CACHE[key] = (time.time() + ttl, val)
    return val


async def _get_session():
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        return _SESSION
    async with _SESSION_LOCK:
        if _SESSION is not None and not _SESSION.closed:
            return _SESSION
        connector = aiohttp.TCPConnector(
            ssl=False, limit=24, ttl_dns_cache=300, enable_cleanup_closed=True
        )
        _SESSION = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=12, connect=5),
            headers=_HEADERS,
        )
        return _SESSION


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS jilu ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, keyword TEXT, time INTEGER, summary TEXT)'
    )
    conn.commit()
    conn.close()


init_db()


@on_unload
async def _close_http():
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


def _btns(*rows):
    return [[{'text': t, 'data': d, 'type': 2} for t, d in row] for row in rows]


def _inline(cmd, label=None):
    return f'[{label or cmd}](mqqapi://aio/inlinecmd?command={quote(cmd)}&enter=false&reply=false)'


def _ms(start):
    return int((time.time() - start) * 1000)


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _power_of(level, wind=None):
    lv = str(level or '').strip()
    if lv in ('TD', 'TS', 'STS', 'TY', 'STY', 'SuperTY', 'SUPERTY'):
        return {'TD': 6, 'TS': 8, 'STS': 10, 'TY': 12, 'STY': 14, 'SuperTY': 16, 'SUPERTY': 16}[lv]
    try:
        return int(float(wind) / 2.5) if wind is not None else 0
    except (TypeError, ValueError):
        return 0


def _fmt_time(v):
    s = str(v or '')
    if len(s) >= 12 and s.isdigit():
        return f'{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}'
    return s


def _avg_radius(wind_radius, code):
    for row in wind_radius or []:
        if not row or str(row[0]).upper() != code:
            continue
        vals = []
        for v in row[1:5]:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
        if vals:
            return round(sum(vals) / len(vals))
    return None


def parse_jsonp(text):
    text = (text or '').strip()
    m = re.search(r'^[^(]*\(\s*(\{.*\})\s*\)\s*;?\s*$', text, re.S)
    if m:
        return json.loads(m.group(1))
    i, j = text.find('{'), text.rfind('}')
    if i >= 0 and j > i:
        return json.loads(text[i:j + 1])
    raise ValueError('无法解析 JSONP')


def _clean_name(v):
    s = str(v or '').strip()
    if not s or s.lower() in ('null', 'none', 'nameless', '未知'):
        return ''
    return s


def _level_label(level):
    return _LEVEL_CN.get(str(level or '').strip(), str(level or '-'))


def _wind_txt(wind):
    try:
        ms = float(wind)
        return f'{wind}m/s({ms * 3.6:.0f}km/h)'
    except (TypeError, ValueError):
        return f'{wind or "-"}m/s'


def _png_wh(data):
    if data and len(data) >= 24 and data[:8] == b'\x89PNG\r\n\x1a\n':
        import struct
        return struct.unpack('>II', data[16:24])
    if data and len(data) > 100 and data[:2] == b'\xff\xd8':
        return (900, 700)
    return (900, 700)


async def http_bytes(url, timeout=15, headers=None):
    try:
        session = await _get_session()
        to = aiohttp.ClientTimeout(total=timeout, connect=5)
        hdrs = headers if headers is not None else _HEADERS
        async with session.get(url, timeout=to, headers=hdrs) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception as e:
        log.warning('下载失败 %s: %s', url, e)
        return None


async def http_text(url, timeout=12, headers=None):
    try:
        session = await _get_session()
        to = aiohttp.ClientTimeout(total=timeout, connect=5)
        hdrs = headers if headers is not None else _HEADERS
        async with session.get(url, timeout=to, headers=hdrs) as resp:
            if resp.status != 200:
                return None
            return await resp.text()
    except Exception as e:
        log.warning('请求失败 %s: %s', url, e)
        return None


async def nmc_list(year=None):
    key = 'default' if year is None else str(year)
    ck = f'list:{key}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    t = int(time.time() * 1000)
    url = f'{NMC}/list_{key}?t={t}&callback=typhoon_jsons_list_{key}'
    text = await http_text(url, timeout=10)
    if not text:
        return None
    try:
        data = parse_jsonp(text)
    except Exception as e:
        log.warning('解析列表失败: %s', e)
        return None
    rows = []
    for item in data.get('typhoonList') or []:
        if not isinstance(item, (list, tuple)) or len(item) < 8:
            continue
        rows.append({
            'id': item[0],
            'en': _clean_name(item[1]),
            'cn': _clean_name(item[2]),
            'num': str(item[3] or ''),
            'desc': _clean_name(item[6]),
            'status': item[7] or '',
        })
    return _cache_set(ck, {'year': year, 'list': rows}, _LIST_TTL)


async def nmc_view(tid):
    tid = str(tid)
    ck = f'view:{tid}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    t = int(time.time() * 1000)
    url = f'{NMC}/view_{tid}?t={t}&callback=typhoon_jsons_view_{tid}'
    text = await http_text(url, timeout=12)
    if not text:
        return None
    try:
        data = parse_jsonp(text)
    except Exception as e:
        log.warning('解析详情失败: %s', e)
        return None
    ty = data.get('typhoon')
    if not isinstance(ty, (list, tuple)) or len(ty) < 9:
        return None
    points = []
    for p in ty[8] or []:
        if not isinstance(p, (list, tuple)) or len(p) < 8:
            continue
        level = str(p[3] or '').strip()
        wind = p[7]
        wr = p[10] if len(p) > 10 else []
        forecasts = []
        fcmap = p[11] if len(p) > 11 else None
        if isinstance(fcmap, dict) and str(ty[7] if len(ty) > 7 else '') != 'stop':
            for f in fcmap.get('BABJ') or []:
                if not isinstance(f, (list, tuple)) or len(f) < 6:
                    continue
                flvl = str(f[7] if len(f) > 7 else '').strip()
                forecasts.append({
                    'hour': f[0], 'lng': f[2], 'lat': f[3],
                    'pressure': f[4], 'wind': f[5], 'level': flvl,
                })
        points.append({
            'time': _fmt_time(p[1]),
            'level': level,
            'strong': _level_label(level),
            'lng': p[4], 'lat': p[5],
            'pressure': p[6], 'wind': wind,
            'move': p[8] if len(p) > 8 else '',
            'movespeed': p[9] if len(p) > 9 else '',
            'radius7': _avg_radius(wr, '30KTS'),
            'radius10': _avg_radius(wr, '50KTS'),
            'radius12': _avg_radius(wr, '64KTS'),
            'forecasts': forecasts,
        })
    result = {
        'id': ty[0],
        'en': _clean_name(ty[1]),
        'cn': _clean_name(ty[2]),
        'num': str(ty[3] or ''),
        'status': ty[7] if len(ty) > 7 else '',
        'points': points,
    }
    return _cache_set(ck, result, _VIEW_TTL)


def _match(item, keyword):
    kw = keyword.strip()
    if not kw:
        return False
    sid, num = str(item.get('id') or ''), str(item.get('num') or '')
    cn, en = str(item.get('cn') or ''), str(item.get('en') or '')
    if kw == sid or kw == num:
        return True
    if kw.isdigit() and num.isdigit():
        if kw == num or (len(kw) >= 4 and (num.endswith(kw[-4:]) or kw.endswith(num))):
            return True
        if len(kw) <= 2 and len(num) >= 2 and num.endswith(kw.zfill(2)):
            return True
    if kw in cn or (en and kw.lower() in en.lower()):
        return True
    return False


async def resolve_id(keyword):
    kw = keyword.strip()
    if not kw:
        return None, '请输入名称 / 编号 / ID'
    if re.fullmatch(r'\d{6,10}', kw):
        view = await nmc_view(kw)
        if view:
            return view['id'], None
    bundle = await nmc_list()
    if bundle is None:
        return None, '中央气象台接口无响应'
    for it in bundle['list']:
        if it['status'] == 'start' and _match(it, kw):
            return it['id'], None
    for it in bundle['list']:
        if _match(it, kw):
            return it['id'], None
    m = re.search(r'(19|20)\d{2}', kw)
    year = int(m.group(0)) if m else None
    if year:
        yb = await nmc_list(year)
        if yb:
            for it in yb['list']:
                if _match(it, kw):
                    return it['id'], None
    if kw.isdigit() and year is None:
        now_y = datetime.now().year
        ybs = await asyncio.gather(nmc_list(now_y), nmc_list(now_y - 1), return_exceptions=True)
        for yb in ybs:
            if isinstance(yb, Exception) or not yb:
                continue
            for it in yb['list']:
                if _match(it, kw):
                    return it['id'], None
    return None, '未找到该台风'


# ---------- 官网路径产品图 ----------

def _parse_pub_page(html):
    """从中央台发布页解析台风名 + 最新路径图 URL。"""
    title = ''
    m = re.search(r'<title>([^<]+)</title>', html or '', re.I)
    if m:
        title = m.group(1)
    # 标题形如：台风快讯_台风路径预报_沙德尔
    cn = ''
    mt = re.search(r'路径预报[_．.\s]*([\u4e00-\u9fffA-Za-z0-9·]{2,20})', title)
    if mt:
        cn = mt.group(1).strip()
    imgs = re.findall(r'data-img="(https?://image\.nmc\.cn/product/[^"]+)"', html or '')
    if not imgs:
        imgs = re.findall(r'(https?://image\.nmc\.cn/product/[^"\'?\s]+TCBU[^"\'?\s]+\.(?:JPG|jpg|PNG|png))', html or '')
    codes = sorted(set(re.findall(r'0W\d{6,10}', html or '')))
    latest = imgs[0] if imgs else ''
    if latest:
        latest = latest.split('?')[0].replace('/medium/', '/')
    return {'cn': cn, 'title': title, 'img': latest, 'codes': codes, 'count': len(imgs)}


def _official_match(info, cn, num):
    icn = info.get('cn') or ''
    codes = info.get('codes') or []
    if cn and icn and (cn in icn or icn in cn):
        return True
    if not num:
        return False
    n4 = num[-4:] if len(num) >= 4 else num
    for c in codes:
        if 'null' in c.lower():
            continue
        if num in c or (len(n4) >= 2 and n4 in c):
            return True
    return False


async def nmc_official_maps():
    """并行拉取各发布页索引（短缓存）。"""
    cached = _cache_get('official_maps')
    if cached is not None:
        return cached

    async def one(page):
        html = await http_text(f'{NMC_PUB}/{page}', timeout=8, headers=_PUB_HEADERS)
        if not html:
            return None
        info = _parse_pub_page(html)
        if not info.get('img'):
            return None
        cn = info.get('cn') or ''
        if cn in ('ll号台风', '号台风') or 'null' in (info.get('img') or '').lower():
            if not any('null' not in c.lower() for c in (info.get('codes') or [])):
                return None
        info['page'] = page
        return info

    parts = await asyncio.gather(*[one(p) for p in _PUB_PAGES], return_exceptions=True)
    out, seen = [], set()
    for info in parts:
        if isinstance(info, Exception) or not info:
            continue
        if info['img'] in seen:
            continue
        seen.add(info['img'])
        out.append(info)
    return _cache_set('official_maps', out, _OFFICIAL_TTL)


async def fetch_official_track_png(view):
    """匹配并下载官方路径产品图；停编直接跳过。"""
    if view.get('status') == 'stop':
        return None, '暂无该台风的中央台官方路径产品图（停编）'
    cn = view.get('cn') or ''
    num = str(view.get('num') or '')
    ck = f'offimg:{view.get("id") or cn or num}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    maps = await nmc_official_maps()
    hit = None
    for info in maps:
        if _official_match(info, cn, num):
            hit = info
            break
    if not hit:
        return None, '暂无该台风的中央台官方路径产品图'
    data = await http_bytes(hit['img'], timeout=15, headers=_PUB_HEADERS)
    if not data or len(data) < 1000:
        mid = hit['img'].replace('/TCBU/', '/TCBU/medium/')
        if '/medium/medium/' not in mid:
            data = await http_bytes(mid, timeout=12, headers=_PUB_HEADERS)
    if not data or len(data) < 1000:
        return None, '官方路径图下载失败'
    result = (data, hit.get('img'))
    _cache_set(ck, result, _IMG_TTL)
    return result


async def upload_track_image(event, png, filename='typhoon_nmc.jpg'):
    try:
        from core.application import get_app
        app = get_app()
        hosting = app.module_manager.get('image_hosting') if app and app.module_manager else None
        if not hosting:
            return None
        bot = app.get_bot(event.appid)
        return await hosting.upload_any(
            png, filename, token_manager=getattr(bot, 'token_manager', None) if bot else None
        )
    except Exception as e:
        log.warning('图床上传失败: %s', e)
        return None


def fmt_list_active(rows):
    active = [x for x in rows if x['status'] == 'start']
    if not active:
        return '当前暂无活跃台风\n可用：台风官网列表 2024'
    md = f'**中央气象台官网 · 活跃台风（{len(active)}）**\n\n'
    for it in active:
        link = _inline(f'台风官网查询 {it["id"]}', it['cn'] or it['en'] or str(it['id']))
        en = f'（{it["en"]}）' if it.get('en') else ''
        md += f'- {link}{en}`{it["num"]}`\n'
    return md


def fmt_year(bundle):
    year = bundle.get('year') or '本年'
    rows = bundle.get('list') or []
    md = f'**中央气象台官网 · {year}（{len(rows)}）**\n\n'
    for it in rows[:30]:
        st = '活跃' if it['status'] == 'start' else '停编'
        link = _inline(f'台风官网查询 {it["id"]}', it['cn'] or it['en'] or '未命名')
        en = f' {it["en"]}' if it.get('en') else ''
        md += f'- {link} `{it["num"]}`{en} · {st}\n'
    if len(rows) > 30:
        md += f'\n…其余 {len(rows) - 30} 个请精确查询'
    return md


def fmt_detail(view, *, image_md='', map_note=''):
    cn, en = view.get('cn') or '', view.get('en') or ''
    title = f'**{cn}**' + (f'（{en}）' if en and en != cn else '')
    pts = view.get('points') or []
    if not pts:
        return title + '\n暂无路径点'
    p = pts[-1]
    lines = [title]
    if view.get('num'):
        lines.append(f'编号：{view.get("num")}')
    lines += [
        f'状态：{"活跃" if view.get("status") == "start" else "停编"}',
        f'强度：{p.get("strong") or "-"}',
        f'气压：{p.get("pressure", "-")} hPa',
        f'风速：{_wind_txt(p.get("wind"))}',
        f'位置：{p.get("lat")}°N {p.get("lng")}°E',
    ]
    if p.get('move'):
        mv = str(p.get('move'))
        if p.get('movespeed') not in (None, ''):
            mv += f' {p.get("movespeed")} km/h'
        lines.append(f'移向：{mv}')
    rparts = [f'{lab}{p.get(k)}km' for lab, k in (('7级', 'radius7'), ('10级', 'radius10'), ('12级', 'radius12')) if p.get(k)]
    if rparts:
        lines.append('风圈：' + ' / '.join(rparts))
    if p.get('time'):
        lines.append(f'时间：{p.get("time")}（北京时）')
    fc = p.get('forecasts') or []
    if fc:
        lines.append('预报：' + ' · '.join(
            f'+{f.get("hour")}h {_LEVEL_CN.get(f.get("level"), f.get("level") or "")}' for f in fc[:5]
        ))
    if map_note:
        lines.append(map_note)
    md = '\n'.join(lines)
    if image_md:
        md += f'\n\n| 中央台官方路径图 |\n| :---: |\n| {image_md} |'
    return md


async def reply_detail(event, view, buttons, *, t0=None):
    png = image_md = None
    map_note = ''
    try:
        png, src = await fetch_official_track_png(view)
        if isinstance(src, str) and src.startswith('http'):
            map_note = '配图：中央气象台官方路径产品图'
        elif isinstance(src, str):
            map_note = src
            png = None
    except Exception as e:
        log.warning('官网路径图失败: %s', e)
        map_note = '官方路径图获取失败'
        png = None
    if png:
        url = await upload_track_image(event, png)
        if url:
            w, h = _png_wh(png)
            image_md = f'![台风路径 #{w}px #{h}px]({url})'
    suffix = f'\n耗时 {_ms(t0)}ms' if t0 is not None else ''
    md = fmt_detail(view, image_md=image_md or '', map_note=map_note) + suffix
    if image_md:
        try:
            await event.reply(md, buttons=buttons, msg_type=2, skip_suffix=True, force_verify_image_resource=True)
            return True
        except Exception as e:
            log.warning('带图回复失败: %s', e)
    await safe_reply(event, fmt_detail(view, map_note=map_note) + suffix, buttons)
    if png and not image_md:
        try:
            await event.reply_image(png)
        except Exception as e:
            log.warning('路径图直发失败: %s', e)
    return True


async def safe_reply(event, text, buttons=None):
    text = (text or '').strip() or '（无内容）'
    for kwargs in (
        {'buttons': buttons, 'msg_type': 2, 'skip_suffix': True},
        {'msg_type': 2, 'skip_suffix': True},
        {},
    ):
        try:
            await event.reply(text if kwargs else text[:800], **kwargs)
            return
        except Exception as e:
            log.warning('回复失败 %s: %s', kwargs, e)


def guard(fn):
    async def wrapper(event, match):
        try:
            return await fn(event, match)
        except Exception as e:
            log.error('%s\n%s', e, traceback.format_exc())
            await safe_reply(event, f'台风官网指令出错：{type(e).__name__}: {e}')
    wrapper.__name__ = fn.__name__
    return wrapper


def save_jilu(uid, keyword, view):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            'INSERT INTO jilu (user, keyword, time, summary) VALUES (?, ?, ?, ?)',
            (uid, keyword, int(time.time()), f'{view.get("cn")}({view.get("num") or view.get("id")})'),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------- 指令（与「台风」本地绘图版并存，前缀不同） ----------

@handler(r'^\s*/?台风官网\s*$', name='台风官网', desc='官网图：当前最强活跃台风', ignore_at_check=True)
@guard
async def cmd_strongest(event, match):
    start = time.time()
    bundle = await nmc_list()
    if bundle is None:
        await safe_reply(event, '中央气象台接口无响应，请稍后重试')
        return
    active = [x for x in bundle['list'] if x['status'] == 'start']
    if not active:
        await safe_reply(event, '当前暂无活跃台风')
        return
    views = await asyncio.gather(*[nmc_view(it['id']) for it in active], return_exceptions=True)
    best, best_view, best_wind = None, None, -1
    for it, view in zip(active, views):
        if isinstance(view, Exception) or not view or not view.get('points'):
            continue
        w = _to_float(view['points'][-1].get('wind')) or 0
        if w >= best_wind:
            best_wind, best, best_view = w, it, view
    if not best_view:
        await safe_reply(event, '活跃台风详情获取失败')
        return
    await reply_detail(
        event, best_view,
        _btns(
            [('刷新', '台风官网'), ('列表', '台风官网列表')],
            [('查询', '台风官网查询 '), ('帮助', '台风官网帮助')],
        ),
        t0=start,
    )


@handler(r'^\s*/?台风官网(?:列表|活跃)(?:\s+(\d{4}))?\s*$', name='台风官网列表', desc='官网图：活跃或按年列表', ignore_at_check=True)
@guard
async def cmd_list(event, match):
    start = time.time()
    year = int(match.group(1)) if match.group(1) else None
    bundle = await nmc_list(year)
    if bundle is None:
        await safe_reply(event, '中央气象台接口无响应，请稍后重试')
        return
    md = (fmt_year(bundle) if year else fmt_list_active(bundle['list'])) + f'\n\n耗时：{_ms(start)}ms'
    await safe_reply(event, md, _btns(
        [('刷新', f'台风官网列表 {year}' if year else '台风官网列表'), ('本年', '台风官网年份')],
        [('查询', '台风官网查询 '), ('帮助', '台风官网帮助')],
    ))


@handler(r'^\s*/?台风官网年份(?:\s+(\d{4}))?\s*$', name='台风官网年份', desc='官网图：按年列表', ignore_at_check=True)
@guard
async def cmd_year(event, match):
    start = time.time()
    year = int(match.group(1)) if match.group(1) else datetime.now().year
    bundle = await nmc_list(year)
    if bundle is None:
        await safe_reply(event, '中央气象台接口无响应，请稍后重试')
        return
    bundle['year'] = year
    md = fmt_year(bundle) + f'\n\n耗时：{_ms(start)}ms'
    await safe_reply(event, md, _btns(
        [('刷新', f'台风官网年份 {year}'), ('活跃', '台风官网列表')],
        [('查询', '台风官网查询 '), ('帮助', '台风官网帮助')],
    ))


@handler(r'^\s*/?台风官网查询\s*(.+?)\s*$', name='台风官网详情', desc='官网图：查详情与官方路径图', ignore_at_check=True)
@guard
async def cmd_detail(event, match):
    start = time.time()
    keyword = (match.group(1) or '').strip()
    if not keyword:
        await safe_reply(event, '请输入名称/编号/ID，例如：台风官网查询 沙德尔')
        return
    if re.fullmatch(r'(19|20)\d{2}', keyword):
        bundle = await nmc_list(int(keyword))
        if bundle is None:
            await safe_reply(event, '中央气象台接口无响应，请稍后重试')
            return
        bundle['year'] = int(keyword)
        md = fmt_year(bundle) + f'\n\n耗时：{_ms(start)}ms'
        await safe_reply(event, md, _btns(
            [('刷新', f'台风官网查询 {keyword}'), ('活跃', '台风官网列表')],
            [('查询', '台风官网查询 '), ('帮助', '台风官网帮助')],
        ))
        return
    tid, err = await resolve_id(keyword)
    if tid is None:
        await safe_reply(event, err or '未找到')
        return
    view = await nmc_view(tid)
    if not view:
        await safe_reply(event, f'详情获取失败（ID {tid}）')
        return
    save_jilu(event.user_id, keyword, view)
    await reply_detail(
        event, view,
        _btns(
            [('刷新', f'台风官网查询 {keyword}'), ('列表', '台风官网列表')],
            [('查询', '台风官网查询 '), ('帮助', '台风官网帮助')],
        ),
        t0=start,
    )


@handler(r'^\s*/?台风官网帮助\s*$', name='台风官网帮助', desc='官网图帮助', ignore_at_check=True)
@guard
async def cmd_help(event, match):
    await safe_reply(event, (
        '**台风官网图 v1.0.0 · 帮助**\n\n'
        f'- {_inline("台风官网")}：当前最强活跃台风 + 官网路径图\n'
        f'- {_inline("台风官网列表")}：活跃列表\n'
        f'- {_inline("台风官网年份")} / `台风官网年份 2024`：按年\n'
        f'- `台风官网查询 沙德尔`：详情 + 中央台官方路径产品图\n\n'
        '文字数据：typhoon.nmc.cn JSON\n'
        '路径配图：www.nmc.cn 发布的 TCBU 官方产品图（非本地绘制）\n'
        '本地绘图版请用：`台风本地` / 合并版请用：`台风`\n'
        '原创：茉莉奶绿 · 修改优化：飞行漂绒'
    ), _btns([('官网台风', '台风官网'), ('合并版', '台风帮助')]))
