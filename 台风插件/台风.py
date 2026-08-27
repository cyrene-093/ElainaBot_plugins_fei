__plugin_meta__ = {
    'name': '台风图',
    'author': '茉莉奶绿（原创） / 飞行漂绒（修改优化）',
    'description': '中央气象台台风查询（最强/活跃出图，停编走 Markdown）',
    'version': '1.2.0',
}

import asyncio
import hashlib
import io
import json
import os
import re
import time
import traceback
from datetime import datetime
from urllib.parse import quote

import aiohttp
from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_unload

log = get_logger(PLUGIN, '台风图')

NMC = 'https://typhoon.nmc.cn/weatherservice/typhoon/jsons'
NMC_PUB = 'https://www.nmc.cn/publish/typhoon'
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
_IMG_DIR = os.path.join(DATA_DIR, 'official_img')
os.makedirs(_IMG_DIR, exist_ok=True)

_LEVEL_CN = {
    'TD': '热带低压', 'TS': '热带风暴', 'STS': '强热带风暴',
    'TY': '台风', 'STY': '强台风', 'SuperTY': '超强台风',
}
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
    'Referer': 'https://typhoon.nmc.cn/web.html',
    'Accept': '*/*',
}
_PUB_HEADERS = {**_HEADERS, 'Referer': f'{NMC_PUB}/probability.html'}
_PUB_PAGES = ('probability.html',) + tuple(f'probability-img{i}.html' for i in range(2, 9))
_SESSION = None
_SESSION_LOCK = asyncio.Lock()
_MEM, _LOCKS = {}, {}
_LIST_TTL, _VIEW_TTL, _MAP_TTL, _IMG_TTL = 60, 90, 180, 180  # 图：磁盘/图床 3 分钟
_PAGE_SIZE = 12
_FONT_CANDIDATES = (
    'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyh.ttf', 'C:/Windows/Fonts/simhei.ttf',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/System/Library/Fonts/PingFang.ttc',
)
_FONT_CACHE = {}
_INK, _MUTED, _LINE, _BG = (30, 41, 59), (100, 116, 139), (226, 232, 240), (248, 250, 252)
_LIVE, _WHITE = (185, 28, 28), (255, 255, 255)


def _cache_get(key):
    item = _MEM.get(key)
    if not item:
        return None
    exp, val = item
    if time.time() > exp:
        _MEM.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl):
    _MEM[key] = (time.time() + ttl, val)
    return val


async def _get_session():
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        return _SESSION
    async with _SESSION_LOCK:
        if _SESSION is not None and not _SESSION.closed:
            return _SESSION
        _SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False, limit=24, ttl_dns_cache=300, enable_cleanup_closed=True),
            timeout=aiohttp.ClientTimeout(total=12, connect=5),
            headers=_HEADERS,
        )
        return _SESSION


@on_unload
async def _close_http():
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


def _btn(text, data, *, style=1, enter=True):
    item = {'text': str(text)[:16], 'data': str(data), 'type': 2, 'style': style}
    if enter:
        item['enter'] = True
    return item


def _nav_btns():
    return [
        [_btn('最强台风', '最强台风'), _btn('活跃台风', '活跃台风')],
        [_btn('台风列表', '台风列表'), _btn('台风帮助', '台风帮助', style=4)],
    ]


def _list_btns(year, page, pages):
    rows = []
    if pages > 1:
        row = []
        if page > 1:
            row.append(_btn('上一页', f'台风列表 {year} {page - 1}'))
        if page < pages:
            row.append(_btn('下一页', f'台风列表 {year} {page + 1}'))
        if row:
            rows.append(row)
    rows.extend(_nav_btns())
    return rows


def _chip(show, cmd):
    return (
        f'<qqbot-cmd-input text="{quote(str(cmd), safe="")}" '
        f'show="{quote(str(show), safe="")}" reference="false" />'
    )


def _year_bar(selected=None):
    now = datetime.now().year
    chips = [_chip(str(y), f'台风列表 {y}') for y in range(now, now - 6, -1)]
    rows = ['　　'.join(chips[i:i + 3]) for i in range(0, len(chips), 3)]
    return '📅 点选年份查看往年列表\n' + '\n'.join(rows)


def _ms(start):
    return int((time.time() - start) * 1000)


def _cmd_head(match):
    s = re.sub(r'^\s*/?', '', (match.group(0) or '')).strip()
    s = re.sub(r'\s*\d{4}\s*$', '', s)
    return s.split()[0] if s else ''


def _hint_query():
    return (
        '❗ 请补上名称、编号或年份。\n\n'
        '💡 示例：\n'
        '`台风查询 沙德尔`　按名称\n'
        '`台风查询 2411`　按编号\n'
        '`台风列表 2023`　查看该年名单'
    )


def _hint_year():
    return '❗ 请补上四位年份，例如：`台风列表 2023`'


def _hint_miss():
    return '❗ 查询不到，请正确使用。例如：`台风列表 2025`　`台风查询 沙德尔`'


def _hint_noarg(cmd=None):
    return _hint_miss()


def _parse_year(text):
    s = str(text or '').strip()
    if not re.fullmatch(r'(19|20)\d{2}', s):
        return None
    year = int(s)
    now = datetime.now().year
    if year < 1945 or year > now:
        return None
    return year


def _parse_page_token(text):
    s = str(text or '').strip()
    if re.fullmatch(r'(19|20)\d{2}', s):
        return None
    m = re.fullmatch(r'(?:p|第)?([1-9]\d?)页?', s, re.I)
    return int(m.group(1)) if m else None


def _parse_year_page(text, *, default_year=None):
    s = str(text or '').strip()
    page = 1
    if not s:
        return default_year, 1
    parts = s.split()
    tok = _parse_page_token(parts[-1])
    if tok is not None and (len(parts) > 1 or default_year is not None):
        page = tok
        parts = parts[:-1]
        s = ' '.join(parts).strip()
    if not s:
        return default_year, page
    m = re.fullmatch(r'((?:19|20)\d{2})(?:第([1-9]\d?)页)?', s)
    if m:
        year = _parse_year(m.group(1))
        if year is None:
            return None, None
        if m.group(2):
            page = int(m.group(2))
        return year, page
    year = _parse_year(s)
    if year is None:
        return None, None
    return year, page


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_time(v):
    s = str(v or '')
    return f'{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}' if len(s) >= 12 and s.isdigit() else s


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
    return '' if (not s or s.lower() in ('null', 'none', 'nameless', '未知')) else s


def _wind_txt(wind):
    try:
        ms = float(wind)
        return f'{wind}m/s({ms * 3.6:.0f}km/h)'
    except (TypeError, ValueError):
        return f'{wind or "-"}m/s'


def _ok(result):
    if result in (False, None):
        return False
    if isinstance(result, tuple):
        return bool(result) and result[0] is True
    if isinstance(result, dict):
        return result.get('code') in (None, 0, '0')
    return bool(result)


def _lock(key):
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _disk_name(*parts):
    return hashlib.md5('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()


def _disk_load(name):
    path = os.path.join(_IMG_DIR, name)
    try:
        if not os.path.isfile(path) or time.time() - os.path.getmtime(path) > _IMG_TTL:
            return None
        with open(path, 'rb') as f:
            data = f.read()
        return data if data and len(data) > 800 else None
    except Exception:
        return None


def _disk_save(name, data):
    path = os.path.join(_IMG_DIR, name)
    try:
        with open(path, 'wb') as f:
            f.write(data)
        files = [os.path.join(_IMG_DIR, n) for n in os.listdir(_IMG_DIR) if n.endswith(('.jpg', '.bin'))]
        if len(files) > 48:
            files.sort(key=os.path.getmtime)
            for p in files[: len(files) - 36]:
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception as e:
        log.warning('图片缓存写入失败: %s', e)


def _info_pairs(view):
    pts = view.get('points') or []
    p = pts[-1] if pts else {}
    pairs = [
        ('编号', str(view.get('num') or '-')),
        ('状态', '活跃' if view.get('status') == 'start' else '停编'),
        ('强度', str(p.get('strong') or '-')),
        ('气压', f'{p.get("pressure", "-")} hPa'),
        ('风速', _wind_txt(p.get('wind'))),
        ('当前位置', f'{p.get("lat")}°N  {p.get("lng")}°E'),
    ]
    if p.get('move'):
        mv = str(p.get('move'))
        if p.get('movespeed') not in (None, ''):
            mv += f'  {p.get("movespeed")} km/h'
        pairs.append(('移向', mv))
    rparts = [f'{lab}{p.get(k)}km' for lab, k in (('7级', 'radius7'), ('10级', 'radius10'), ('12级', 'radius12')) if p.get(k)]
    if rparts:
        pairs.append(('风圈', ' / '.join(rparts)))
    if p.get('time'):
        pairs.append(('时间', f'{p.get("time")}（北京时）'))
    return pairs


def _forecast_line(view, sep='  ·  '):
    pts = view.get('points') or []
    fc = (pts[-1].get('forecasts') if pts else None) or []
    if not fc:
        return ''
    bits = sep.join(f'+{f.get("hour")}h {_LEVEL_CN.get(f.get("level"), f.get("level") or "")}' for f in fc[:5])
    return f'预报  {bits}'


def _defense_tips(view):
    pts = view.get('points') or []
    if view.get('status') == 'stop' or not pts:
        return []
    p = pts[-1]
    wind = _to_float(p.get('wind')) or 0
    level = str(p.get('level') or '')
    strong = p.get('strong') or _LEVEL_CN.get(level, '') or ''
    if wind >= 41 or level in ('STY', 'SuperTY', 'SUPERTY') or '强台风' in strong or '超强' in strong:
        return ['请留在坚固建筑物内，远离门窗玻璃', '停止户外及水上作业，服从转移安排', '备足饮用水与照明，避免涉水出行']
    if wind >= 24.5 or level in ('TY', 'STY', 'SuperTY') or '台风' in strong:
        return ['尽量减少外出，关闭门窗并收妥阳台物品', '远离工地、广告牌及高大树木', '低洼地区注意内涝，切勿涉水通行']
    return ['外出注意大风和强降雨', '关闭门窗，移走阳台易坠物', '积水勿趟，远离树木和广告牌']


async def _http(url, *, binary=False, timeout=12, headers=None):
    try:
        session = await _get_session()
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout, connect=5), headers=headers or _HEADERS
        ) as resp:
            if resp.status != 200:
                return None
            return await (resp.read() if binary else resp.text())
    except Exception as e:
        log.warning('请求失败 %s: %s', url, e)
        return None


async def nmc_list(year=None):
    key = 'default' if year is None else str(year)
    ck = f'list:{key}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    text = await _http(f'{NMC}/list_{key}?t={int(time.time() * 1000)}&callback=typhoon_jsons_list_{key}', timeout=10)
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
            'id': item[0], 'en': _clean_name(item[1]), 'cn': _clean_name(item[2]),
            'num': str(item[3] or ''), 'status': item[7] or '',
        })
    return _cache_set(ck, {'year': year, 'list': rows}, _LIST_TTL)


async def nmc_view(tid):
    tid = str(tid)
    ck = f'view:{tid}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    text = await _http(f'{NMC}/view_{tid}?t={int(time.time() * 1000)}&callback=typhoon_jsons_view_{tid}')
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
    points, stopped = [], str(ty[7] if len(ty) > 7 else '') == 'stop'
    for p in ty[8] or []:
        if not isinstance(p, (list, tuple)) or len(p) < 8:
            continue
        level, wr = str(p[3] or '').strip(), p[10] if len(p) > 10 else []
        forecasts, fcmap = [], p[11] if len(p) > 11 else None
        if isinstance(fcmap, dict) and not stopped:
            for f in fcmap.get('BABJ') or []:
                if not isinstance(f, (list, tuple)) or len(f) < 6:
                    continue
                forecasts.append({
                    'hour': f[0], 'lng': f[2], 'lat': f[3], 'pressure': f[4], 'wind': f[5],
                    'level': str(f[7] if len(f) > 7 else '').strip(),
                })
        points.append({
            'time': _fmt_time(p[1]), 'level': level,
            'strong': _LEVEL_CN.get(level, level or '-'),
            'lng': p[4], 'lat': p[5], 'pressure': p[6], 'wind': p[7],
            'move': p[8] if len(p) > 8 else '', 'movespeed': p[9] if len(p) > 9 else '',
            'radius7': _avg_radius(wr, '30KTS'), 'radius10': _avg_radius(wr, '50KTS'),
            'radius12': _avg_radius(wr, '64KTS'), 'forecasts': forecasts,
        })
    return _cache_set(ck, {
        'id': ty[0], 'en': _clean_name(ty[1]), 'cn': _clean_name(ty[2]),
        'num': str(ty[3] or ''), 'status': ty[7] if len(ty) > 7 else '', 'points': points,
    }, _VIEW_TTL)


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
    if len(kw) >= 2 and (kw in cn or (en and kw.lower() in en.lower())):
        return True
    return False


async def resolve_id(keyword):
    kw = keyword.strip()
    if not kw:
        return None, '请输入台风名称或编号，例如：台风查询 沙德尔'
    if re.fullmatch(r'\d{6,10}', kw):
        view = await nmc_view(kw)
        if view:
            return view['id'], None
    bundle = await nmc_list()
    if bundle is None:
        return None, '暂时无法获取台风数据，请稍后重试'
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
        for yb in await asyncio.gather(nmc_list(now_y), nmc_list(now_y - 1), return_exceptions=True):
            if isinstance(yb, Exception) or not yb:
                continue
            for it in yb['list']:
                if _match(it, kw):
                    return it['id'], None
    return None, '未查询到该台风'


def _parse_pub_page(html):
    html = html or ''
    m = re.search(r'<title>([^<]+)</title>', html, re.I)
    mt = re.search(r'路径预报[_．.\s]*([\u4e00-\u9fffA-Za-z0-9·]{2,20})', m.group(1) if m else '')
    imgs = re.findall(r'data-img="(https?://image\.nmc\.cn/product/[^"]+)"', html)
    if not imgs:
        imgs = re.findall(r'(https?://image\.nmc\.cn/product/[^"\'?\s]+TCBU[^"\'?\s]+\.(?:JPG|jpg|PNG|png))', html)
    latest = (imgs[0].split('?')[0].replace('/medium/', '/') if imgs else '')
    return {
        'cn': mt.group(1).strip() if mt else '',
        'img': latest,
        'codes': sorted(set(re.findall(r'0W\d{6,10}', html))),
    }


def _official_match(info, cn, num):
    icn = info.get('cn') or ''
    if cn and icn and (cn in icn or icn in cn):
        return True
    if not num:
        return False
    n4 = num[-4:] if len(num) >= 4 else num
    for c in info.get('codes') or []:
        if 'null' not in c.lower() and (num in c or (len(n4) >= 2 and n4 in c)):
            return True
    return False


async def nmc_official_maps():
    cached = _cache_get('official_maps')
    if cached is not None:
        return cached

    async def one(page):
        html = await _http(f'{NMC_PUB}/{page}', timeout=8, headers=_PUB_HEADERS)
        if not html:
            return None
        info = _parse_pub_page(html)
        if not info.get('img'):
            return None
        cn, img = info.get('cn') or '', info.get('img') or ''
        if (cn in ('ll号台风', '号台风') or 'null' in img.lower()) and not any(
            'null' not in c.lower() for c in (info.get('codes') or [])
        ):
            return None
        return info

    parts = await asyncio.gather(*[one(p) for p in _PUB_PAGES], return_exceptions=True)
    out, seen = [], set()
    for info in parts:
        if isinstance(info, Exception) or not info or info['img'] in seen:
            continue
        seen.add(info['img'])
        out.append(info)
    return _cache_set('official_maps', out, _MAP_TTL)


async def fetch_official_track_png(view):
    if view.get('status') == 'stop':
        return None, ''
    cn, num = view.get('cn') or '', str(view.get('num') or '')
    maps = await nmc_official_maps()
    hit = next((i for i in maps if _official_match(i, cn, num)), None)
    if not hit:
        return None, ''
    src = hit.get('img') or ''
    name = _disk_name('rawo', src) + '.bin'
    async with _lock(src):
        data = _disk_load(name)
        if not data:
            data = await _http(src, binary=True, timeout=18, headers=_PUB_HEADERS)
            if not data or len(data) < 2000:
                mid = src.replace('/TCBU/', '/TCBU/medium/')
                if mid != src and '/medium/medium/' not in mid:
                    data = await _http(mid, binary=True, timeout=12, headers=_PUB_HEADERS)
            if data and len(data) >= 2000:
                _disk_save(name, data)
    return (data, src) if data and len(data) >= 2000 else (None, '')


def _name(event):
    n = str(getattr(event, 'username', '') or '').strip()
    if not n or n.isdigit() or (len(n) >= 18 and n.isalnum()):
        return '你'
    return n


def _avatar(event):
    for key in ('avatar', 'avatar_url', 'head_img', 'avatarUrl'):
        v = str(getattr(event, key, '') or '').strip()
        if v.startswith(('http://', 'https://')):
            return v
    appid = str(getattr(event, 'appid', '') or '').strip()
    oid = str(getattr(event, 'raw_user_id', None) or getattr(event, 'user_id', '') or '').strip()
    if appid and oid:
        return f'https://q.qlogo.cn/qqapp/{appid}/{oid}/100'
    return ''


def _font(size):
    size = int(size)
    hit = _FONT_CACHE.get(size)
    if hit is not None:
        return hit
    font = None
    try:
        from PIL import ImageFont
        for path in _FONT_CANDIDATES:
            if os.path.isfile(path):
                font = ImageFont.truetype(path, size=size)
                break
        if font is None:
            font = ImageFont.load_default()
    except Exception:
        font = None
    _FONT_CACHE[size] = font
    return font


def _tw(draw, text, font):
    text = text or ''
    if not font:
        return max(len(text) * 8, 1), 12
    if hasattr(draw, 'textbbox'):
        b = draw.textbbox((0, 0), text, font=font)
        return b[2] - b[0], b[3] - b[1]
    return draw.textsize(text, font=font)


def _line_h(draw, font, extra=8):
    size = getattr(font, 'size', None) or 20
    h = _tw(draw, '国Agyp', font)[1]
    return max(int(h + extra), int(size * 1.42))


def _ellipsize(draw, text, font, max_w):
    text = str(text or '')
    if max_w <= 0:
        return ''
    if _tw(draw, text, font)[0] <= max_w:
        return text
    ell = '…'
    if _tw(draw, ell, font)[0] >= max_w:
        return ell
    out = ''
    for ch in text:
        if _tw(draw, out + ch + ell, font)[0] > max_w:
            break
        out += ch
    return (out or text[:1]) + ell


def _metrics(W):
    W = max(int(W), 640)
    pad = max(10, min(16, W * 10 // 880))
    ft = max(22, min(34, W * 26 // 880))
    fs = max(20, min(32, W * 24 // 880))
    fm = max(16, min(24, W * 18 // 880))
    return pad, ft, fs, fm


def _jpeg(im):
    rgb = im.convert('RGB')
    buf = io.BytesIO()
    rgb.save(buf, format='JPEG', quality=92, subsampling=0, optimize=True)
    blob = buf.getvalue()
    if len(blob) > 4_000_000:
        buf = io.BytesIO()
        rgb.save(buf, format='JPEG', quality=84, subsampling=0, optimize=True)
        blob = buf.getvalue()
    return blob, rgb.size


def _wrap(draw, text, font, max_w):
    text = str(text or '')
    if not text:
        return ['']
    lines, cur = [], ''
    for ch in text:
        if ch == '\n':
            lines.append(cur)
            cur = ''
            continue
        nxt = cur + ch
        if cur and _tw(draw, nxt, font)[0] > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur = nxt
    if cur or not lines:
        lines.append(cur)
    return lines


def _draw_title(draw, W, title, right, pad, ft, fm):
    title = str(title or ' ')
    right = str(right or '')
    th = _tw(draw, title, _font(ft))[1]
    rh = _tw(draw, right, _font(fm))[1] if right else 0
    gap = 8
    hh = gap * 2 + max(th, rh)
    draw.rectangle((0, 0, W, hh), fill=_WHITE)
    rw = _tw(draw, right, _font(fm))[0] if right else 0
    title_max = max(40, W - pad * 2 - (rw + 28 if right else 0))
    title = _ellipsize(draw, title, _font(ft), title_max)
    draw.text((pad, gap), title, font=_font(ft), fill=_INK)
    if right:
        rw, rh = _tw(draw, right, _font(fm))
        draw.text((W - pad - rw, gap + max((th - rh) // 2, 0)), right, font=_font(fm), fill=_MUTED)
    draw.line((0, hh - 1, W, hh - 1), fill=_LINE)
    return hh


def compose_detail(map_blob, view, note=''):
    from PIL import Image, ImageDraw
    mp = None
    if map_blob:
        try:
            mp = Image.open(io.BytesIO(map_blob)).convert('RGB')
        except Exception:
            mp = None
    W = max(mp.size[0] if mp else 880, 800)
    pad, ft, fs, fm = _metrics(W)
    probe = ImageDraw.Draw(Image.new('RGB', (W, 8)))
    cn, en = view.get('cn') or '', view.get('en') or ''
    heading = cn or en or '台风'
    st = '活跃' if view.get('status') == 'start' else '停编'
    right = '  '.join(x for x in (str(view.get('num') or ''), st) if x)

    shorts, longs = [], []
    for k, v in _info_pairs(view):
        (longs if k in ('当前位置', '风圈', '时间') else shorts).append((k, str(v)))
    fc = _forecast_line(view, sep='  ')
    if fc:
        longs.append(('预报', fc.replace('预报  ', '', 1).strip()))
    tips = _defense_tips(view)

    col_w = (W - pad * 3) // 2
    font_s = _font(fs)
    lh = _line_h(probe, font_s, 8)
    kept, rest = [], []
    for k, v in shorts:
        lw = _tw(probe, k, font_s)[0] + 8
        (rest if _tw(probe, v, font_s)[0] > max(col_w - lw - 4, 24) else kept).append((k, v))
    shorts, longs = kept, rest + longs
    long_blocks = []
    for k, v in longs:
        lw = _tw(probe, k, font_s)[0] + 10
        long_blocks.append((k, lw, _wrap(probe, v, font_s, max(40, W - pad * 2 - lw))))
    lab_w = _tw(probe, '防护', font_s)[0] + 10
    tip_w = max(40, W - pad * 2 - lab_w)
    tip_blocks = [_wrap(probe, f'{i}. {t}', font_s, tip_w) for i, t in enumerate(tips[:3], 1)]

    mh = mp.size[1] if mp else 0
    canvas = Image.new('RGB', (W, 80 + mh + 1400), _BG)
    draw = ImageDraw.Draw(canvas)
    y = _draw_title(draw, W, heading, right, pad, ft, fm)
    if en and cn and en != cn:
        sub_h = _line_h(probe, _font(fm), 4)
        draw.rectangle((0, y, W, y + sub_h + 6), fill=_WHITE)
        en_max = W - pad * 2 - (_tw(draw, note, _font(fm))[0] + 16 if note else 0)
        draw.text((pad, y), _ellipsize(draw, en, _font(fm), en_max), font=_font(fm), fill=_MUTED)
        if note:
            nw, _ = _tw(draw, note, _font(fm))
            draw.text((W - pad - nw, y), note, font=_font(fm), fill=_MUTED)
        y += sub_h + 6
        draw.line((0, y - 1, W, y - 1), fill=_LINE)
    if mp:
        canvas.paste(mp, ((W - mp.size[0]) // 2, y))
        y += mh
    y += 10
    for i in range(0, len(shorts), 2):
        row = shorts[i:i + 2]
        for col, (k, v) in enumerate(row):
            x0 = pad + col * (col_w + pad)
            lw = _tw(draw, k, font_s)[0] + 8
            draw.text((x0, y), k, font=font_s, fill=_MUTED)
            vmax = max(24, col_w - lw)
            draw.text((x0 + lw, y), _ellipsize(draw, v, font_s, vmax), font=font_s, fill=_INK)
        y += lh
    if shorts and long_blocks:
        y += 4
    for k, lw, wrapped in long_blocks:
        draw.text((pad, y), k, font=font_s, fill=_MUTED)
        vx = pad + lw
        for j, line in enumerate(wrapped):
            draw.text((vx, y + j * lh), line, font=font_s, fill=_INK)
        y += max(len(wrapped), 1) * lh + 2
    if tip_blocks:
        y += 4
        draw.line((pad, y, W - pad, y), fill=_LINE)
        y += 8
        vx = pad + lab_w
        draw.text((pad, y), '防护', font=font_s, fill=_MUTED)
        for block in tip_blocks:
            for j, line in enumerate(block):
                draw.text((vx, y), line, font=font_s, fill=_INK)
                y += lh
            y += 2
    return _jpeg(canvas.crop((0, 0, W, min(canvas.size[1], y + 10))))


def compose_card(spec, note=''):
    from PIL import Image, ImageDraw
    W = 880
    pad, ft, fs, fm = _metrics(W)
    probe = ImageDraw.Draw(Image.new('RGB', (W, 8)))
    kind = spec.get('kind') or 'lines'
    title = spec.get('title') or '台风'
    rows = spec.get('rows') or []
    lines = spec.get('lines') or []
    canvas = Image.new('RGB', (W, 3600), _BG)
    draw = ImageDraw.Draw(canvas)
    y = _draw_title(draw, W, title, note, pad, ft, fm) + 8
    lh = _line_h(probe, _font(fs), 8)
    name_lh = _line_h(probe, _font(ft), 10)
    meta_lh = _line_h(probe, _font(fm), 4)
    if kind == 'year':
        nx = pad
        namx = pad + _tw(probe, '00000', _font(fs))[0] + 12
        for num, name, st in rows:
            sw, _ = _tw(draw, st, _font(fs))
            name_max = max(40, W - pad - namx - sw - 16)
            draw.text((nx, y), str(num), font=_font(fs), fill=_MUTED)
            draw.text((namx, y), _ellipsize(draw, name, _font(fs), name_max), font=_font(fs), fill=_INK)
            draw.text((W - pad - sw, y), st, font=_font(fs), fill=_LIVE if st == '活跃' else _MUTED)
            y += lh
    elif kind == 'active':
        for name, meta, pos in rows:
            for wln in _wrap(probe, name, _font(ft), W - pad * 2):
                draw.text((pad, y), wln, font=_font(ft), fill=_INK)
                y += name_lh
            if meta:
                draw.text((pad, y), _ellipsize(draw, meta, _font(fm), W - pad * 2), font=_font(fm), fill=_MUTED)
                y += meta_lh
            if pos:
                draw.text((pad, y), _ellipsize(draw, pos, _font(fm), W - pad * 2), font=_font(fm), fill=_MUTED)
                y += meta_lh + 6
            else:
                y += 8
    elif kind == 'help':
        cmd_w = max((_tw(probe, a, _font(fs))[0] for a, _ in rows), default=80) + 4
        for cmd, desc in rows:
            draw.text((pad, y), cmd, font=_font(fs), fill=_INK)
            draw.text((pad + cmd_w + 12, y), desc, font=_font(fs), fill=_MUTED)
            y += lh
        y += 4
        for ln in lines:
            fill = _MUTED if ln in ('示例',) or ln.endswith('：') else _INK
            for wln in _wrap(probe, ln, _font(fs), W - pad * 2):
                draw.text((pad, y), wln, font=_font(fs), fill=fill)
                y += lh
    else:
        for ln in lines:
            if not str(ln).strip():
                y += 4
                continue
            for wln in _wrap(probe, ln, _font(fs), W - pad * 2):
                draw.text((pad, y), wln, font=_font(fs), fill=_INK)
                y += lh
    return _jpeg(canvas.crop((0, 0, W, min(3600, y + 8))))


def _head(event):
    name = f'@{_name(event)}'
    av = _avatar(event)
    return f'![头像 #24px #24px]({av}) {name}' if av else name


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


async def _sender_of(event):
    sender = getattr(event, 'sender', None)
    if sender:
        return sender
    try:
        from core.application import get_app
        app = get_app()
        bot = app.get_bot(event.appid) if app else None
        return getattr(bot, 'sender', None) if bot else None
    except Exception:
        return None


async def _send_native(event, blob, filename, buttons=None):
    sender = await _sender_of(event)
    if not sender:
        return False
    try:
        fi = await sender.upload_media(event, blob, 1, file_name=filename)
        if not fi:
            return False
        kw = {'media': {'file_info': fi}, 'skip_suffix': True}
        if buttons:
            kw['buttons'] = buttons
        return _ok(await event.reply(' ', **kw))
    except Exception as e:
        log.warning('媒体上传失败: %s', e)
        return False


async def _send_merged(event, blob, size, cache_key, buttons=None, extra=''):
    w, h = size
    url = _cache_get(f'host:{cache_key}') if cache_key else None
    if not url:
        url = await upload_track_image(event, blob, filename='typhoon_nmc.jpg')
        if url and cache_key:
            _cache_set(f'host:{cache_key}', url, _IMG_TTL)
    if not url:
        return False
    md = f'{_head(event)}\n![路径 #{w}px #{h}px]({url})'
    if extra:
        md += f'\n\n{extra}'
    for btns in ((buttons or None), None):
        for force in (False, True):
            try:
                kw = {'msg_type': 2, 'skip_suffix': True, 'force_verify_image_resource': force}
                if btns:
                    kw['buttons'] = btns
                r = await event.reply(md, **kw)
                if _ok(r):
                    log.info('路径图合并消息 %sx%s force=%s buttons=%s', w, h, force, bool(btns))
                    return True
                log.warning('合并 Markdown 未成功 force=%s: %s', force, r)
            except Exception as e:
                log.warning('合并 Markdown 失败 force=%s: %s', force, e)
    return False


async def _send_pic(event, blob, size, buttons=None, cache_key=None, src='', extra=''):
    w, h = size
    if await _send_merged(event, blob, (w, h), cache_key, buttons, extra):
        return True
    if await _send_native(event, blob, 'typhoon_nmc.jpg', buttons):
        log.info('路径图原图通道 %sx%s %sB src=%s', w, h, len(blob), src)
        if extra:
            await safe_reply(event, extra, buttons)
        return True
    try:
        sent = _ok(await event.reply_image(blob, ''))
    except Exception as e:
        log.warning('reply_image 失败: %s', e)
        sent = False
    if sent:
        follow = extra or ('快捷操作' if buttons else '')
        if follow or buttons:
            await safe_reply(event, follow or '快捷操作', buttons)
    return sent


def _pos_txt(view):
    pts = (view or {}).get('points') or []
    if not pts:
        return ''
    p = pts[-1]
    lat, lng = p.get('lat'), p.get('lng')
    if lat in (None, '') or lng in (None, ''):
        return ''
    return f'{lat}°N {lng}°E'


def spec_lines(title, text):
    s = str(text or '').replace('**', '').replace('`', '')
    s = re.sub(r'\n{3,}', '\n\n', s).strip()
    return {'kind': 'lines', 'title': title, 'lines': s.split('\n')}


def spec_active(active, views=None):
    views = views or [None] * len(active)
    rows = []
    for it, view in zip(active, views):
        name = it['cn'] or it['en'] or str(it['id'])
        bits = [str(it.get('num') or '')]
        if it.get('en') and it.get('en') != it.get('cn'):
            bits.append(it['en'])
        pos = _pos_txt(view) if view and not isinstance(view, Exception) else ''
        rows.append((name, ' · '.join(x for x in bits if x), f'当前位置  {pos}' if pos else ''))
    return {'kind': 'active', 'title': f'活跃台风（{len(active)}）', 'rows': rows}


def spec_year(bundle):
    year = bundle.get('year') or datetime.now().year
    rows = []
    for it in (bundle.get('list') or [])[:30]:
        name = it['cn'] or it['en'] or '未命名'
        if it.get('en') and it.get('en') != it.get('cn'):
            name = f'{name}  {it["en"]}'
        st = '活跃' if it['status'] == 'start' else '停编'
        rows.append((it.get('num') or '', name, st))
    n = len(bundle.get('list') or [])
    return {'kind': 'year', 'title': f'{year}年台风（{n}）', 'rows': rows}


def spec_help():
    return {
        'kind': 'help',
        'title': '台风查询',
        'rows': [
            ('当前台风', '查看当前最强台风'),
            ('活跃台风', '查看全部活跃台风'),
            ('本年台风', '查看本年台风名单'),
            ('台风查询', '按名称或编号查询'),
        ],
        'lines': [
            '示例',
            '台风查询 沙德尔　　按名称',
            '台风查询 2411　　按编号',
            '台风列表 2023　　往年名单',
        ],
    }


def fmt_list_active(active, views=None):
    if not active:
        return '📡 当前暂无活跃台风'
    md = f'**📡 活跃台风（{len(active)}）**\n点选名称查看路径\n\n'
    views = views or [None] * len(active)
    for it, view in zip(active, views):
        link = _chip('🌀 ' + (it['cn'] or it['en'] or str(it['id'])), f'台风查询 {it["id"]}')
        bits = [f'`{it["num"]}`']
        if it.get('en') and it.get('en') != it.get('cn'):
            bits.append(it['en'])
        pos = _pos_txt(view) if view and not isinstance(view, Exception) else ''
        md += f'- {link}\n  {" · ".join(bits)}'
        if pos:
            md += f'\n  📍 {pos}'
        md += '\n'
    return md


def _page_bar(year, page, pages):
    if pages <= 1:
        return ''
    chips = [_chip(f'·{i}·' if i == page else str(i), f'台风列表 {year} {i}') for i in range(1, pages + 1)]
    return f'📄 第 {page}/{pages} 页　' + '　'.join(chips)


def fmt_year(bundle, page=1):
    year = bundle.get('year') or datetime.now().year
    rows = bundle.get('list') or []
    total = len(rows)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(1, int(page or 1)), pages)
    chunk = rows[(page - 1) * _PAGE_SIZE: page * _PAGE_SIZE]
    md = f'**📋 {year}年台风（{total}）**'
    if pages > 1:
        md += f'　第{page}/{pages}页'
    md += '\n点选名称查看路径\n————————————\n\n'
    for it in chunk:
        link = _chip('🌀 ' + (it['cn'] or it['en'] or '未命名'), f'台风查询 {it["id"]}')
        live = it['status'] == 'start'
        st = '🔴 活跃' if live else '⚫ 停编'
        bits = [f'`{it["num"]}`', st]
        if it.get('en') and it.get('en') != it.get('cn'):
            bits.insert(1, it['en'])
        md += f'- {link}\n  {" · ".join(bits)}\n'
    bar = _page_bar(year, page, pages)
    if bar:
        md += '\n' + bar
    md += '\n\n' + _year_bar(year)
    return md, page, pages


_MD_EMOJI = {
    '编号': '🔢', '强度': '💪', '气压': '📉', '风速': '💨',
    '当前位置': '📍', '最后位置': '📍', '移向': '🧭', '风圈': '⭕',
    '时间': '🕐', '生成': '🌱', '停编时间': '🕐', '过程最强': '⚡',
}


def _peak_point(view):
    best, best_w = None, -1
    for p in view.get('points') or []:
        w = _to_float(p.get('wind')) or 0
        if w >= best_w:
            best, best_w = p, w
    return best or {}


def fmt_detail(view):
    cn, en = view.get('cn') or '', view.get('en') or ''
    live = view.get('status') == 'start'
    st = '🔴 活跃' if live else '⚫ 停编'
    title = f'🌀 **{cn or "未命名"}**' + (f'（{en}）' if en and en != cn else '')
    num = view.get('num') or ''
    lines = [title, f'`{num}`　{st}' if num else st, '————————————']
    pts = view.get('points') or []
    if not pts:
        return '\n'.join(lines + ['暂无路径点'])
    if not live:
        lines.append('📝 该台风已停编，以下为监测资料。')
    last, peak = pts[-1], _peak_point(view)
    skip = {'状态', '时间'} if not live else {'状态'}
    for k, v in _info_pairs(view):
        if k in skip:
            continue
        if k == '当前位置' and not live:
            k = '最后位置'
        lines.append(f'{_MD_EMOJI.get(k, "•")} {k}：{v}')
    if not live:
        first_t, last_t = pts[0].get('time'), last.get('time')
        if first_t:
            lines.append(f'{_MD_EMOJI["生成"]} 生成：{first_t}（北京时）')
        if last_t:
            lines.append(f'{_MD_EMOJI["停编时间"]} 停编时间：{last_t}（北京时）')
        pw, lw = _to_float(peak.get('wind')) or 0, _to_float(last.get('wind')) or 0
        if pw > lw + 0.5:
            bits = [peak.get('strong') or '-', _wind_txt(peak.get('wind'))]
            if peak.get('time'):
                bits.append(str(peak.get('time')))
            lines.append(f'{_MD_EMOJI["过程最强"]} 过程最强：{" · ".join(bits)}')
    fc = _forecast_line(view, sep=' · ')
    if fc and live:
        lines.append('🔮 ' + fc.replace('预报  ', '预报：', 1))
    tips = _defense_tips(view)
    if tips:
        lines += ['', '💡 **防护建议**'] + [f'{i}. {t}' for i, t in enumerate(tips[:3], 1)]
    return '\n'.join(lines)


async def _prepared_image(view):
    try:
        raw, src = await fetch_official_track_png(view)
    except Exception as e:
        log.warning('官网路径图失败: %s', e)
        return None, None, ''
    if not raw or not str(src).startswith('http'):
        return None, None, ''
    try:
        from PIL import Image
        size = Image.open(io.BytesIO(raw)).size
    except Exception as e:
        log.warning('官网图无法读取: %s', e)
        return None, None, ''
    return raw, size, src


async def reply_detail(event, view, *, t0=None, buttons=None):
    buttons = buttons if buttons is not None else _nav_btns()
    live = view.get('status') == 'start'
    if not live:
        text = fmt_detail(view)
        if t0 is not None:
            text += f'\n\n耗时：{_ms(t0)}ms'
        await safe_reply(event, text, buttons)
        return True
    blob = src = None
    try:
        blob, _, src = await _prepared_image(view)
    except Exception as e:
        log.warning('官网图准备失败: %s', e)
    note = f'{_ms(t0)}ms' if t0 is not None else ''
    card = size = None
    try:
        card, size = await asyncio.to_thread(compose_detail, blob, view, note)
    except Exception as e:
        log.warning('详情卡片渲染失败: %s', e)
    if card and size:
        pts = view.get('points') or []
        last_t = (pts[-1].get('time') if pts else '') or ''
        host_key = _disk_name('card', view.get('id'), last_t, len(card))
        if await _send_pic(event, card, size, buttons, cache_key=host_key, src=src):
            return True
        log.warning('详情卡片发送失败，改发文字')
    await safe_reply(event, fmt_detail(view), buttons)
    return True


async def safe_reply(event, text, buttons=None):
    text = (text or '').strip() or '（无内容）'
    md = f'{_head(event)}\n{text}'
    for btns in ((buttons or None), None):
        try:
            kw = {'msg_type': 2, 'skip_suffix': True}
            if btns:
                kw['buttons'] = btns
            r = await event.reply(md, **kw)
            if _ok(r):
                return
            log.warning('回复未成功: %s', r)
        except Exception as e:
            log.warning('回复失败 markdown: %s', e)
    try:
        await event.reply(f'{_head(event)}\n{text}'[:800], skip_suffix=True)
    except Exception as e:
        log.warning('回复失败: %s', e)


def guard(fn):
    async def wrapper(event, match):
        try:
            return await fn(event, match)
        except Exception as e:
            log.error('%s\n%s', e, traceback.format_exc())
            await safe_reply(event, f'台风指令出错：{type(e).__name__}: {e}', _nav_btns())
    wrapper.__name__ = fn.__name__
    return wrapper


async def _say(event, text, t0=None, *, buttons=None, extra=''):
    if extra:
        text = f'{(text or "").strip()}\n\n{extra}'
    if t0 is not None:
        text = f'{text}\n\n耗时：{_ms(t0)}ms'
    await safe_reply(event, text, buttons if buttons is not None else _nav_btns())


async def _say_card(event, spec, extra='', t0=None, buttons=None):
    btns = buttons if buttons is not None else _nav_btns()
    note = f'{_ms(t0)}ms' if t0 is not None else ''
    card = size = None
    try:
        card, size = await asyncio.to_thread(compose_card, spec, note)
    except Exception as e:
        log.warning('文字卡片渲染失败: %s', e)
    if card and size:
        key = _disk_name('tcard', spec.get('title'), len(card))
        if await _send_pic(event, card, size, btns, cache_key=key, extra=extra):
            return
    text = spec.get('md') or spec.get('title') or '台风'
    await _say(event, text, t0=t0, buttons=btns, extra='' if spec.get('md') else extra)


# ---------- 指令 ----------

def _arg(match):
    try:
        return (match.group(1) or '').strip()
    except IndexError:
        return ''


async def _reply_year_list(event, year, page=1, t0=None):
    bundle = await nmc_list(year)
    if bundle is None:
        return await _say(event, '❗ 暂时无法获取台风数据，请稍后重试')
    bundle['year'] = year
    if not bundle.get('list'):
        return await _say(event, _hint_miss(), extra=_year_bar(year))
    md, page, pages = fmt_year(bundle, page)
    await _say(event, md, t0=t0, buttons=_list_btns(year, page, pages))


@handler(r'^\s*/?(?:台风|当前台风|最强台风)(?:\s+(\S.*?))?\s*$', name='台风', desc='查看当前最强台风', ignore_at_check=True, block=True)
@guard
async def cmd_strongest(event, match):
    extra = _arg(match)
    if extra:
        return await _say(event, _hint_miss())
    start = time.time()
    bundle = await nmc_list()
    if bundle is None:
        return await _say(event, '❗ 暂时无法获取台风数据，请稍后重试')
    active = [x for x in bundle['list'] if x['status'] == 'start']
    if not active:
        return await _say(event, '📡 当前暂无活跃台风', extra=_year_bar(), t0=start)
    views = await asyncio.gather(*[nmc_view(it['id']) for it in active], return_exceptions=True)
    best_view, best_wind = None, -1
    for view in views:
        if isinstance(view, Exception) or not view or not view.get('points'):
            continue
        w = _to_float(view['points'][-1].get('wind')) or 0
        if w >= best_wind:
            best_wind, best_view = w, view
    if not best_view:
        return await _say(event, '❗ 暂时无法获取该台风详情')
    await reply_detail(event, best_view, t0=start)


@handler(r'^\s*/?(?:台风活跃|活跃台风)(?:\s*(\S.*?))?\s*$', name='活跃台风', desc='查看活跃台风', ignore_at_check=True, block=True)
@guard
async def cmd_list(event, match):
    extra = _arg(match)
    if extra:
        year, page = _parse_year_page(extra)
        if year is None:
            return await _say(event, _hint_miss())
        return await _reply_year_list(event, year, page, t0=time.time())
    year = None
    start = time.time()
    bundle = await nmc_list(year)
    if bundle is None:
        return await _say(event, '❗ 暂时无法获取台风数据，请稍后重试')
    active = [x for x in bundle['list'] if x['status'] == 'start']
    views = await asyncio.gather(*[nmc_view(it['id']) for it in active], return_exceptions=True) if active else []
    if not active:
        return await _say(event, '📡 当前暂无活跃台风', extra=_year_bar(), t0=start)
    chips = []
    for it in active:
        show = it['cn'] or it['en'] or str(it['id'])
        chips.append(_chip('🌀 ' + show, f'台风查询 {it["id"]}'))
    extra = '点选名称查看路径\n' + '\n'.join(chips)
    spec = spec_active(active, views)
    spec['md'] = fmt_list_active(active, views)
    await _say_card(event, spec, extra=extra, t0=start)


@handler(r'^\s*/?(?:台风列表|台风年份|今年台风|本年台风)(?:\s*(\S.*?))?\s*$', name='台风年份', desc='查看本年台风', ignore_at_check=True, block=True)
@guard
async def cmd_year(event, match):
    extra = _arg(match)
    head = _cmd_head(match)
    implied = head in ('今年台风', '本年台风')
    if not extra and not implied:
        return await _say(event, _hint_year(), extra=_year_bar())
    year, page = _parse_year_page(extra, default_year=datetime.now().year if implied else None)
    if year is None:
        return await _say(event, _hint_miss())
    await _reply_year_list(event, year, page, t0=time.time())


@handler(r'^\s*/?(?:台风查询|查台风)(?:\s*(\S.*?))?\s*$', name='台风详情', desc='按名称或编号查询台风', ignore_at_check=True, block=True)
@guard
async def cmd_detail(event, match):
    keyword = _arg(match)
    if not keyword:
        return await _say(event, _hint_query(), extra=_year_bar())
    start = time.time()
    year, page = _parse_year_page(keyword)
    if year is not None:
        return await _reply_year_list(event, year, page, t0=start)
    if re.fullmatch(r'(19|20)\d{2}', keyword):
        return await _say(event, _hint_miss())
    tid, err = await resolve_id(keyword)
    if tid is None:
        return await _say(event, _hint_miss())
    view = await nmc_view(tid)
    if not view:
        return await _say(event, _hint_miss())
    await reply_detail(event, view, t0=start)


@handler(r'^\s*/?(?:台风帮助|台风怎么用|使用说明)(?:\s+(\S.*?))?\s*$', name='台风帮助', desc='使用说明', ignore_at_check=True, block=True)
@guard
async def cmd_help(event, match):
    if _arg(match):
        await _say(event, _hint_noarg('台风帮助'))
        return
    await _say(event, (
        '**🌀 台风查询**\n'
        '————————————\n'
        f'{_chip("🌀 当前台风", "台风")}　查看当前最强台风\n'
        f'{_chip("📡 活跃台风", "活跃台风")}　查看全部活跃台风\n'
        f'{_chip("📋 本年台风", "本年台风")}　查看本年台风名单\n'
        f'{_chip("🔍 台风查询", "台风查询 ")}　按名称或编号查询\n'
        '————————————\n'
        '💡 示例：`台风查询 沙德尔`　`台风查询 2411`\n'
        '📅 往年名单：`台风列表 2023`\n\n'
        + _year_bar()
    ), buttons=[])
