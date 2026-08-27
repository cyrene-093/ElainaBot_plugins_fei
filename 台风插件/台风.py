__plugin_meta__ = {
    'name': '台风图',
    'author': '茉莉奶绿（原创） / 飞行漂绒（修改优化）',
    'description': '中央气象台台风查询（有官网图则出图，没有则 Markdown 文字）',
    'version': '1.3.1',
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
_FONT_CANDIDATES = (
    'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyh.ttf', 'C:/Windows/Fonts/simhei.ttf',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/System/Library/Fonts/PingFang.ttc',
)

_SESSION = None
_SESSION_LOCK = asyncio.Lock()
_MEM, _FONTS, _LOCKS = {}, {}, {}
_LIST_TTL, _VIEW_TTL, _MAP_TTL, _IMG_TTL = 60, 90, 180, 180  # 图：磁盘/图床 3 分钟


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


def _chip(show, cmd):
    return (
        f'<qqbot-cmd-input text="{quote(str(cmd), safe="")}" '
        f'show="{quote(str(show), safe="")}" reference="false" />'
    )


def _nav(refresh='台风', extra=None):
    a, b, c, d = (
        _chip('最强台风', '最强台风'),
        _chip('活跃台风', '活跃台风'),
        _chip('台风列表', '台风列表'),
        _chip('台风帮助', '台风帮助'),
    )
    return f'────────\n{a}　　{b}\n{c}　　{d}'


def _ms(start):
    return int((time.time() - start) * 1000)


def _year_bar(selected=None):
    now = datetime.now().year
    chips = [_chip(str(y), f'台风列表 {y}') for y in range(now, now - 6, -1)]
    rows = ['　　'.join(chips[i:i + 3]) for i in range(0, len(chips), 3)]
    return '点选年份查看往年列表\n' + '\n'.join(rows)


def _cmd_head(match):
    s = re.sub(r'^\s*/?', '', (match.group(0) or '')).strip()
    s = re.sub(r'\s*\d{4}\s*$', '', s)
    return s.split()[0] if s else ''


def _hint_query():
    return (
        '请补上名称、编号或年份。\n\n'
        '示例：\n'
        '`台风查询 沙德尔`　按名称\n'
        '`台风查询 2411`　按编号\n'
        '`台风列表 2023`　查看该年名单\n\n'
        + _year_bar()
    )


def _hint_year():
    return '请补上四位年份，例如：`台风列表 2023`\n\n' + _year_bar()


def _hint_miss():
    return '查询不到，请正确使用。例如：`台风列表 2025`　`台风查询 沙德尔`'


def _parse_year(text):
    s = str(text or '').strip()
    if not re.fullmatch(r'(19|20)\d{2}', s):
        return None
    year = int(s)
    now = datetime.now().year
    if year < 1945 or year > now:
        return None
    return year


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


def _font(size):
    hit = _FONTS.get(size)
    if hit:
        return hit
    try:
        from PIL import ImageFont
        for p in _FONT_CANDIDATES:
            if os.path.isfile(p):
                try:
                    font = ImageFont.truetype(p, size)
                    _FONTS[size] = font
                    return font
                except Exception:
                    continue
        font = ImageFont.load_default()
        _FONTS[size] = font
        return font
    except Exception:
        return None


def _tw(draw, text, font):
    if hasattr(draw, 'textbbox'):
        b = draw.textbbox((0, 0), text, font=font)
        return b[2] - b[0], b[3] - b[1]
    return len(text) * 7, 12


def _trim_white(im, limit=248, pad=6):
    g = im.convert('L')
    w, h = g.size
    px = g.load()
    step_x, step_y = max(1, w // 70), max(1, h // 70)

    def row_bg(y):
        return all(px[x, y] >= limit for x in range(0, w, step_x))

    def col_bg(x):
        return all(px[x, y] >= limit for y in range(0, h, step_y))

    top, bot, left, right = 0, h - 1, 0, w - 1
    while top < h - 24 and row_bg(top):
        top += 1
    while bot > top + 24 and row_bg(bot):
        bot -= 1
    while left < w - 24 and col_bg(left):
        left += 1
    while right > left + 24 and col_bg(right):
        right -= 1
    box = (max(0, left - pad), max(0, top - pad), min(w, right + 1 + pad), min(h, bot + 1 + pad))
    nw, nh = box[2] - box[0], box[3] - box[1]
    if nw < w * 0.58 or nh < h * 0.58 or nw * nh > 0.96 * w * h:
        return im
    return im.crop(box)


def _title_bits(view):
    cn, en = view.get('cn') or '', view.get('en') or ''
    return (cn or en or '台风'), (en if en and cn and en != cn else '')


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


def _wrap(draw, text, font, max_w):
    text = str(text or '')
    if not text:
        return []
    if not font:
        return [text]
    lines, cur = [], ''
    for ch in text:
        if cur and _tw(draw, cur + ch, font)[0] > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines or [text]


def compose_official(data, view, *, max_side=720):
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(data)).convert('RGB')
    im = _trim_white(im)
    w, h = im.size
    m = max(w, h) or 1
    if m > max_side:
        im = im.resize((max(1, int(w * max_side / m)), max(1, int(h * max_side / m))), Image.Resampling.BILINEAR)
        w, h = im.size

    title, sub = _title_bits(view)
    pairs, fc_text, tips = _info_pairs(view), _forecast_line(view), _defense_tips(view)
    tf, lf = _font(24) or _font(22), _font(15) or _font(14)
    vf, sf = _font(16) or _font(15), _font(14) or lf
    probe = ImageDraw.Draw(im)
    pad_x, pad_y = 16, 12
    max_txt = max(48, w - pad_x * 2)
    col_w = max(48, w // 2 - pad_x - 8)
    gap = 8
    title_lines = _wrap(probe, title, tf, max_txt)
    sub_lines = _wrap(probe, sub, sf, max_txt) if sub else []
    pair_rows = []
    for i in range(0, len(pairs), 2):
        cells = []
        for lab, val in pairs[i:i + 2]:
            lw = _tw(probe, lab, lf)[0] if lf else 36
            vmax = max(20, col_w - lw - gap)
            cells.append((lab, lw, _wrap(probe, str(val), vf, vmax) or ['-']))
        pair_rows.append(cells)
    fc_lines = _wrap(probe, fc_text, sf, max_txt) if fc_text else []
    tip_lines = []
    if tips:
        tip_lines.append('防护建议')
        for i, t in enumerate(tips[:3], 1):
            tip_lines.extend(_wrap(probe, f'{i}. {t}', sf, max_txt))
    line_h_t = max((_tw(probe, '国', tf)[1] if tf else 24), 20) + 3
    line_h_v = max((_tw(probe, '国', vf)[1] if vf else 16), 16) + 3
    line_h_s = max((_tw(probe, '国', sf)[1] if sf else 14), 14) + 3
    cap_h = pad_y + line_h_t * max(1, len(title_lines)) + line_h_s * len(sub_lines) + 10
    for cells in pair_rows:
        cap_h += max(len(c[2]) for c in cells) * line_h_v + 2
    if fc_lines:
        cap_h += 6 + line_h_s * len(fc_lines)
    if tip_lines:
        cap_h += 14 + line_h_s * len(tip_lines)
    cap_h += pad_y + 10

    bg = (248, 250, 252)
    out = Image.new('RGB', (w, h + cap_h), bg)
    out.paste(im, (0, 0))
    d = ImageDraw.Draw(out)
    d.line((0, h, w, h), fill=(210, 216, 224), width=2)
    y = h + pad_y
    if tf:
        for line in title_lines:
            d.text((pad_x, y), line, font=tf, fill=(18, 28, 42))
            y += line_h_t
    if sub and sf:
        for line in sub_lines:
            d.text((pad_x, y), line, font=sf, fill=(96, 108, 122))
            y += line_h_s
    d.line((pad_x, y, w - pad_x, y), fill=(226, 230, 236))
    y += 8
    mid = w // 2
    for cells in pair_rows:
        row_h = max(len(c[2]) for c in cells) * line_h_v
        for col, (lab, lw, vlines) in enumerate(cells):
            x = pad_x if col == 0 else mid + 6
            if lf:
                d.text((x, y), lab, font=lf, fill=(118, 128, 142))
            if vf:
                vx = x + lw + gap
                for j, line in enumerate(vlines):
                    d.text((vx, y + j * line_h_v), line, font=vf, fill=(22, 32, 46))
        y += row_h + 2
    if fc_lines:
        y += 4
        for line in fc_lines:
            d.text((pad_x, y), line, font=sf, fill=(72, 84, 98))
            y += line_h_s
    if tip_lines:
        y += 4
        d.line((pad_x, y, w - pad_x, y), fill=(226, 230, 236))
        y += 8
        for i, line in enumerate(tip_lines):
            d.text((pad_x, y), line, font=sf, fill=(46, 92, 138) if i == 0 else (40, 52, 66))
            y += line_h_s

    buf = io.BytesIO()
    out.save(buf, format='JPEG', quality=70)
    return buf.getvalue(), out.size


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
    mid = src.replace('/TCBU/', '/TCBU/medium/')
    name = _disk_name('rawm', src) + '.bin'
    async with _lock(src):
        data = _disk_load(name)
        if not data:
            urls = [mid, src] if mid != src and '/medium/medium/' not in mid else [src]
            for u in urls:
                data = await _http(u, binary=True, timeout=8, headers=_PUB_HEADERS)
                if data and len(data) >= 1000:
                    break
            if data and len(data) >= 1000:
                _disk_save(name, data)
    return (data, src) if data and len(data) >= 1000 else (None, '')


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


def _head(event, *, md=True):
    name = f'@{_name(event)}'
    av = _avatar(event) if md else ''
    return f'![头像 #24px #24px]({av}) {name}' if av else name


async def _send_pic(event, blob, size, footer='', cache_key=None):
    w, h = size
    url = _cache_get(f'host:{cache_key}') if cache_key else None
    if not url:
        url = await upload_track_image(event, blob)
        if url and cache_key:
            _cache_set(f'host:{cache_key}', url, _IMG_TTL)
    head = _head(event)
    if url:
        md = f'{head}\n![路径 #{w}px #{h}px]({url})'
        if footer:
            md += f'\n\n{footer}'
        for force in (True, False):
            try:
                r = await event.reply(md, msg_type=2, skip_suffix=True, force_verify_image_resource=force)
                if _ok(r):
                    return True
            except Exception as e:
                log.warning('合并 Markdown 失败 force=%s: %s', force, e)
    cap = f'{_head(event, md=False)}\n台风路径'
    sent = False
    try:
        sent = _ok(await event.reply_image(blob, cap))
    except Exception as e:
        log.warning('reply_image 失败: %s', e)
    if sent and footer:
        await safe_reply(event, footer)
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


def fmt_list_active(active, views=None):
    if not active:
        return '当前暂无活跃台风\n\n' + _year_bar()
    md = f'**活跃台风（{len(active)}）**\n点选名称查看路径\n\n'
    views = views or [None] * len(active)
    for it, view in zip(active, views):
        link = _chip(it['cn'] or it['en'] or str(it['id']), f'台风查询 {it["id"]}')
        en = f'（{it["en"]}）' if it.get('en') else ''
        pos = _pos_txt(view) if view and not isinstance(view, Exception) else ''
        extra = f'\n  当前位置：{pos}' if pos else ''
        md += f'- {link}{en}`{it["num"]}`{extra}\n'
    return md


def fmt_year(bundle):
    year = bundle.get('year') or datetime.now().year
    rows = bundle.get('list') or []
    md = f'**{year}年台风（{len(rows)}）**\n点选名称查看路径\n\n'
    for it in rows[:30]:
        st = '活跃' if it['status'] == 'start' else '停编'
        link = _chip(it['cn'] or it['en'] or '未命名', f'台风查询 {it["id"]}')
        en = f' {it["en"]}' if it.get('en') else ''
        md += f'- {link} `{it["num"]}`{en} · {st}\n'
    if len(rows) > 30:
        md += f'\n…其余 {len(rows) - 30} 个请精确查询'
    md += '\n\n' + _year_bar(year)
    return md


def fmt_detail(view):
    cn, en = view.get('cn') or '', view.get('en') or ''
    title = f'**{cn}**' + (f'（{en}）' if en and en != cn else '')
    pts = view.get('points') or []
    if not pts:
        return title + '\n暂无路径点'
    lines = [title] + [f'{k}：{v}' for k, v in _info_pairs(view)]
    fc = _forecast_line(view, sep=' · ')
    if fc:
        lines.append(fc.replace('预报  ', '预报：', 1))
    tips = _defense_tips(view)
    if tips:
        lines += ['', '**防护建议**'] + [f'{i}. {t}' for i, t in enumerate(tips[:3], 1)]
    return '\n'.join(lines)


async def _prepared_image(view):
    try:
        raw, src = await fetch_official_track_png(view)
    except Exception as e:
        log.warning('官网路径图失败: %s', e)
        return None, None
    if not raw or not str(src).startswith('http'):
        return None, None
    pts = view.get('points') or []
    last_t = (pts[-1].get('time') if pts else '') or ''
    ck = _disk_name('out7', view.get('id'), src, last_t)
    blob = _disk_load(ck + '.jpg')
    if blob:
        try:
            from PIL import Image
            return blob, Image.open(io.BytesIO(blob)).size
        except Exception:
            blob = None
    try:
        blob, size = await asyncio.to_thread(compose_official, raw, view)
    except Exception as e:
        log.warning('官网图渲染失败: %s', e)
        return None, None
    if blob:
        _disk_save(ck + '.jpg', blob)
        return blob, size
    return None, None


async def reply_detail(event, view, footer='', *, t0=None):
    blob = size = None
    try:
        blob, size = await _prepared_image(view)
    except Exception as e:
        log.warning('官网图准备失败: %s', e)
    if t0 is not None:
        footer = (footer + f'\n耗时 {_ms(t0)}ms').strip() if footer else f'耗时 {_ms(t0)}ms'
    if blob and size:
        pts = view.get('points') or []
        last_t = (pts[-1].get('time') if pts else '') or ''
        host_key = _disk_name('host', view.get('id'), last_t, len(blob))
        if await _send_pic(event, blob, size, footer, cache_key=host_key):
            return True
        log.warning('官网图发送失败，改发文字')
    body = fmt_detail(view)
    await safe_reply(event, f'{body}\n\n{footer}' if footer else body)
    return True


async def safe_reply(event, text, footer=None):
    text = (text or '').strip() or '（无内容）'
    if footer:
        text = f'{text}\n\n{footer}'
    md = f'{_head(event)}\n{text}'
    try:
        r = await event.reply(md, msg_type=2, skip_suffix=True)
        if _ok(r):
            return
        log.warning('回复未成功: %s', r)
    except Exception as e:
        log.warning('回复失败 markdown: %s', e)
    try:
        await event.reply(f'{_head(event, md=False)}\n{text}'[:800], skip_suffix=True)
    except Exception as e:
        log.warning('回复失败: %s', e)


def guard(fn):
    async def wrapper(event, match):
        try:
            return await fn(event, match)
        except Exception as e:
            log.error('%s\n%s', e, traceback.format_exc())
            await safe_reply(event, f'台风指令出错：{type(e).__name__}: {e}')
    wrapper.__name__ = fn.__name__
    return wrapper


async def _say(event, text, refresh='台风', extra=None, t0=None):
    if t0 is not None:
        text = f'{text}\n\n耗时：{_ms(t0)}ms'
    await safe_reply(event, text, _nav(refresh, extra))


# ---------- 指令 ----------

def _arg(match):
    try:
        return (match.group(1) or '').strip()
    except IndexError:
        return ''


@handler(r'^\s*/?(?:台风|当前台风|最强台风)(?:\s+(\S.*?))?\s*$', name='台风', desc='查看当前最强台风', ignore_at_check=True, block=True)
@guard
async def cmd_strongest(event, match):
    extra = _arg(match)
    if extra:
        return await _say(event, _hint_miss())
    start = time.time()
    bundle = await nmc_list()
    if bundle is None:
        return await _say(event, '暂时无法获取台风数据，请稍后重试')
    active = [x for x in bundle['list'] if x['status'] == 'start']
    if not active:
        return await _say(event, '当前暂无活跃台风\n\n' + _year_bar(), extra=('本年台风', '今年台风'))
    views = await asyncio.gather(*[nmc_view(it['id']) for it in active], return_exceptions=True)
    best_view, best_wind = None, -1
    for view in views:
        if isinstance(view, Exception) or not view or not view.get('points'):
            continue
        w = _to_float(view['points'][-1].get('wind')) or 0
        if w >= best_wind:
            best_wind, best_view = w, view
    if not best_view:
        return await _say(event, '暂时无法获取该台风详情')
    await reply_detail(event, best_view, _nav('台风'), t0=start)


@handler(r'^\s*/?(?:台风活跃|活跃台风)(?:\s*(\S.*?))?\s*$', name='活跃台风', desc='查看活跃台风', ignore_at_check=True, block=True)
@guard
async def cmd_list(event, match):
    extra = _arg(match)
    if extra:
        year = _parse_year(extra)
        if year is None:
            return await _say(event, _hint_miss())
    else:
        year = None
    start = time.time()
    bundle = await nmc_list(year)
    if bundle is None:
        return await _say(event, '暂时无法获取台风数据，请稍后重试', refresh='活跃台风')
    if year:
        md = fmt_year(bundle)
    else:
        active = [x for x in bundle['list'] if x['status'] == 'start']
        views = await asyncio.gather(*[nmc_view(it['id']) for it in active], return_exceptions=True) if active else []
        md = fmt_list_active(active, views)
    await _say(event, md, refresh=f'活跃台风 {year}' if year else '活跃台风', extra=('本年台风', '今年台风'), t0=start)


@handler(r'^\s*/?(?:台风列表|台风年份|今年台风|本年台风)(?:\s*(\S.*?))?\s*$', name='台风年份', desc='查看本年台风', ignore_at_check=True, block=True)
@guard
async def cmd_year(event, match):
    extra = _arg(match)
    head = _cmd_head(match)
    implied = head in ('今年台风', '本年台风')
    if not extra and not implied:
        return await _say(event, _hint_year())
    if extra:
        year = _parse_year(extra)
        if year is None:
            return await _say(event, _hint_miss())
    else:
        year = datetime.now().year
    start = time.time()
    bundle = await nmc_list(year)
    if bundle is None:
        return await _say(event, '暂时无法获取台风数据，请稍后重试')
    bundle['year'] = year
    if not bundle.get('list'):
        return await _say(event, _hint_miss())
    now = datetime.now().year
    refresh = '台风列表' if year == now else f'台风列表 {year}'
    await _say(event, fmt_year(bundle), refresh=refresh, t0=start)


@handler(r'^\s*/?(?:台风查询|查台风)(?:\s*(\S.*?))?\s*$', name='台风详情', desc='按名称或编号查询台风', ignore_at_check=True, block=True)
@guard
async def cmd_detail(event, match):
    keyword = _arg(match)
    if not keyword:
        return await _say(event, _hint_query())
    start = time.time()
    year = _parse_year(keyword)
    if year is not None:
        bundle = await nmc_list(year)
        if bundle is None:
            return await _say(event, '暂时无法获取台风数据，请稍后重试')
        bundle['year'] = year
        if not bundle.get('list'):
            return await _say(event, _hint_miss())
        return await _say(event, fmt_year(bundle), refresh=f'台风列表 {year}', t0=start)
    if re.fullmatch(r'(19|20)\d{2}', keyword):
        return await _say(event, _hint_miss())
    tid, err = await resolve_id(keyword)
    if tid is None:
        return await _say(event, _hint_miss())
    view = await nmc_view(tid)
    if not view:
        return await _say(event, _hint_miss())
    await reply_detail(event, view, _nav(f'台风查询 {keyword}'), t0=start)


@handler(r'^\s*/?(?:台风帮助|台风怎么用|使用说明)(?:\s+(\S.*?))?\s*$', name='台风帮助', desc='使用说明', ignore_at_check=True, block=True)
@guard
async def cmd_help(event, match):
    if _arg(match):
        await safe_reply(event, _hint_noarg('台风帮助'))
        return
    await safe_reply(event, (
        '**台风查询**\n\n'
        f'{_chip("当前台风", "台风")}　查看当前最强台风\n'
        f'{_chip("活跃台风", "活跃台风")}　查看全部活跃台风\n'
        f'{_chip("本年台风", "本年台风")}　查看本年台风名单\n'
        f'{_chip("台风查询", "台风查询 ")}　按名称或编号查询\n\n'
        '示例：`台风查询 沙德尔`　`台风查询 2411`\n'
        '往年名单：`台风列表 2023`\n\n'
        f'{_year_bar()}'
    ))
