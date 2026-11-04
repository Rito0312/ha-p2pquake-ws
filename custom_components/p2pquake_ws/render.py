"""緊急地震速報・地震情報の画像を作る（Home Assistant 非依存。Pillow だけを使う）.

受信した JSON から 1280×800 の PNG を作る。画面の大きさは Echo Show 8 に合わせている。
- 緊急地震速報（556）: 左に地図（警報の対象地域を予想震度で塗る）、右に自宅の地域の予想震度などを大きく出す。
- 地震情報（551）: 左に地図（観測された震度で細分区域を塗る）、右に最大震度・自宅の地域の震度・津波の有無など。

地図: 気象庁「地震情報／細分区域」GIS データ（国土地理院の承認を得て作成されたもの）を
簡略化して assets/areas.json にした。座標は経度・緯度の 1/100 度単位の整数。
観測点（市町村）から細分区域への対応: 気象庁「市町村等（地震津波関係）」GIS データの各市町村を、
最も重なる細分区域に割り当てて assets/cities.json にした。
フォント: Noto Sans JP（SIL Open Font License 1.1、assets/OFL.txt）。
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_LOGGER = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
AREAS_PATH = os.path.join(HERE, "assets", "areas.json")
CITIES_PATH = os.path.join(HERE, "assets", "cities.json")
FONT_PATH = os.path.join(HERE, "assets", "NotoSansJP.ttf")

WIDTH, HEIGHT = 1280, 800
HEADER_H = 104
MAP_BOX = (0, HEADER_H, 770, HEIGHT)  # 地図を描く範囲（左, 上, 右, 下）
PANEL_X = 790
PANEL_W = WIDTH - PANEL_X - 28  # 右側の欄の幅

# 本州〜九州・北海道（沖縄・奄美は左上に別枠。小笠原は描かない）
MAIN_EXTENT = (128.4, 146.2, 30.2, 45.7)  # 経度の最小・最大、緯度の最小・最大
INSET_EXTENT = (122.8, 130.4, 24.0, 28.9)
INSET_BOX = (16, HEADER_H + 16, 236, HEADER_H + 176)

C_BG = "#ffffff"
C_SEA = "#dfe7ef"
C_LAND = "#f4f4f2"
C_BORDER = "#b7bcc2"
C_TEXT = "#111111"
C_SUB = "#555555"
C_ALERT = "#d0021b"
C_CANCEL = "#5f6368"
C_INFO = "#1f4e79"
C_UNKNOWN = "#d6d6d6"  # 予想震度が不明（-1）の警報対象地域

# 地図データにはあるが、自宅の地域の選択肢には出さない区域
_NOT_SELECTABLE = {"うるま市・金武町境界部地先の埋立地"}

SCALE_LABEL = {0: "0", 10: "1", 20: "2", 30: "3", 40: "4", 45: "5弱", 50: "5強",
               55: "6弱", 60: "6強", 70: "7"}


def _scale_color(scale: int) -> str:
    """予想震度（上限）の色。気象庁の震度の配色に近づけたもの."""
    if scale >= 70:
        return "#b40068"
    if scale >= 60:
        return "#a50021"
    if scale >= 55:
        return "#ff2800"
    if scale >= 50:
        return "#ff9900"
    if scale >= 45:
        return "#ffe600"
    if scale >= 40:
        return "#fff59d"
    return "#cfe8ff"  # 予想震度3以下（長周期地震動などで対象になった地域）


def _eew_scale(area: dict[str, Any]) -> tuple[int, str, str]:
    """緊急地震速報の地域の予想震度を (並べ替え用の値, 表示, 色) にする.

    scaleTo 99 は「scaleFrom 程度以上」、-1 は不明。
    """
    s_from, s_to = _int(area.get("scaleFrom")), _int(area.get("scaleTo"))
    if s_to == 99 and s_from in SCALE_LABEL:
        return s_from, f"{SCALE_LABEL[s_from]}以上", _scale_color(s_from)
    if s_to in SCALE_LABEL and s_to > 0:
        return s_to, SCALE_LABEL[s_to], _scale_color(s_to)
    if s_from in SCALE_LABEL and s_from > 0:
        return s_from, SCALE_LABEL[s_from], _scale_color(s_from)
    return -1, "不明", C_UNKNOWN


def _text_color_on(bg: str) -> str:
    r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
    return "#ffffff" if (0.299 * r + 0.587 * g + 0.114 * b) < 140 else C_TEXT


@lru_cache(maxsize=1)
def _areas() -> dict[str, list[list[int]]]:
    with open(AREAS_PATH, encoding="utf-8") as f:
        return json.load(f)


def area_names() -> list[str]:
    """地図に載っている細分区域の名前（設定画面の選択肢に使う）.

    気象庁の区域の順（北海道→沖縄）のまま返す（areas.json は気象庁の GIS データの順に並んでいる）。
    """
    return [k for k, v in _areas().items() if v and k not in _NOT_SELECTABLE]


_font_warned = False


@lru_cache(maxsize=64)
def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    global _font_warned
    font = ImageFont.truetype(FONT_PATH, size)
    try:
        font.set_variation_by_name(weight)
    except (OSError, ValueError):
        # このフォントの既定は Thin（極細）なので、太さを変えられないと読みにくくなる
        if not _font_warned:
            _font_warned = True
            _LOGGER.warning("フォントの太さ（%s）を設定できませんでした。文字が細く表示されます", weight)
    return font


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hhmmss(value: Any) -> str:
    """'2026/10/16 12:43:02' → '12:43:02'。読めなければ空文字."""
    text = str(value or "")
    try:
        return datetime.strptime(text[:19], "%Y/%m/%d %H:%M:%S").strftime("%H:%M:%S")
    except ValueError:
        return ""


class _Projector:
    """経度・緯度を、指定した枠の中の画素の位置に変換する（緯度で横を縮める正距円筒図法）."""

    def __init__(self, extent: tuple[float, float, float, float], box: tuple[int, int, int, int]) -> None:
        lon0, lon1, lat0, lat1 = extent
        self.k = math.cos(math.radians((lat0 + lat1) / 2))
        w_deg = (lon1 - lon0) * self.k
        h_deg = lat1 - lat0
        bw, bh = box[2] - box[0], box[3] - box[1]
        self.s = min(bw / w_deg, bh / h_deg)
        self.lon0, self.lat1 = lon0, lat1
        self.ox = box[0] + (bw - w_deg * self.s) / 2
        self.oy = box[1] + (bh - h_deg * self.s) / 2

    def __call__(self, lon: float, lat: float) -> tuple[float, float]:
        return (self.ox + (lon - self.lon0) * self.k * self.s,
                self.oy + (self.lat1 - lat) * self.s)


@lru_cache(maxsize=1)
def _bboxes() -> dict[str, tuple[float, float, float, float]]:
    """区域ごとの範囲（経度の最小・最大、緯度の最小・最大）."""
    out = {}
    for name, rings in _areas().items():
        xs = [x for r in rings for x in r[0::2]]
        ys = [y for r in rings for y in r[1::2]]
        if xs:
            out[name] = (min(xs) / 100, max(xs) / 100, min(ys) / 100, max(ys) / 100)
    return out


def _inside(lon: float, lat: float, extent: tuple[float, float, float, float]) -> bool:
    return extent[0] <= lon <= extent[1] and extent[2] <= lat <= extent[3]


def _map_extent(names: list[str], points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """対象地域と震源が大きく見えるように地図の範囲を決める（日本全体より広くはしない）.

    本州などに対象がなく、沖縄・奄美にだけある場合はそちらを拡大する。
    """
    boxes = _bboxes()
    for region in (MAIN_EXTENT, INSET_EXTENT):
        xs: list[float] = []
        ys: list[float] = []
        for n in names:
            b = boxes.get(n)
            if b and _inside((b[0] + b[1]) / 2, (b[2] + b[3]) / 2, region):
                xs += [b[0], b[1]]
                ys += [b[2], b[3]]
        for lon, lat in points:
            if _inside(lon, lat, region):
                xs.append(lon)
                ys.append(lat)
        if xs:
            break
    if not xs:
        return MAIN_EXTENT
    lat_c = (min(ys) + max(ys)) / 2
    k = math.cos(math.radians(lat_c))
    # 余白を付け、狭くなりすぎないようにする（南北で最低4度）
    h = max((max(ys) - min(ys)) * 1.25 + 0.6, 4.0)
    w = max((max(xs) - min(xs)) * k * 1.25 + 0.6, 0.0)
    bw, bh = MAP_BOX[2] - MAP_BOX[0], MAP_BOX[3] - MAP_BOX[1]
    # 地図の枠と同じ縦横比にする
    if w / h < bw / bh:
        w = h * bw / bh
    else:
        h = w * bh / bw
    if h >= (MAIN_EXTENT[3] - MAIN_EXTENT[2]) * 0.8:
        return MAIN_EXTENT
    lon_c = (min(xs) + max(xs)) / 2
    return (lon_c - w / k / 2, lon_c + w / k / 2, lat_c - h / 2, lat_c + h / 2)


def _draw_areas(draw: ImageDraw.ImageDraw, proj: _Projector, fills: dict[str, str],
                extent: tuple[float, float, float, float], width: int = 1) -> None:
    lon0, lon1, lat0, lat1 = extent
    for name, rings in _areas().items():
        fill = fills.get(name, C_LAND)
        for ring in rings:
            xs, ys = ring[0::2], ring[1::2]
            if max(xs) < lon0 * 100 or min(xs) > lon1 * 100 or max(ys) < lat0 * 100 or min(ys) > lat1 * 100:
                continue
            pts = [proj(x / 100, y / 100) for x, y in zip(xs, ys)]
            draw.polygon(pts, fill=fill, outline=C_BORDER, width=width)


def _outline_area(draw: ImageDraw.ImageDraw, proj: _Projector, name: str, color: str, width: int) -> tuple[float, float] | None:
    """指定した区域を太い線で囲み、いちばん大きい部分の中心を返す."""
    best = None
    for ring in _areas().get(name, []):
        pts = [proj(x / 100, y / 100) for x, y in zip(ring[0::2], ring[1::2])]
        draw.line(pts + [pts[0]], fill=color, width=width, joint="curve")
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        size = len(pts)
        if best is None or size > best[0]:
            best = (size, cx, cy)
    return (best[1], best[2]) if best else None


def _draw_epicenter(draw: ImageDraw.ImageDraw, x: float, y: float, r: int = 16) -> None:
    for w, c in ((11, "#ffffff"), (6, C_ALERT)):
        draw.line([(x - r, y - r), (x + r, y + r)], fill=c, width=w)
        draw.line([(x - r, y + r), (x + r, y - r)], fill=c, width=w)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, start: int, minimum: int, weight: str) -> ImageFont.FreeTypeFont:
    size = start
    while size > minimum and draw.textlength(text, font=_font(size, weight)) > max_w:
        size -= 4
    return _font(size, weight)


def _header(draw: ImageDraw.ImageDraw, title: str, right: str, color: str) -> None:
    draw.rectangle([0, 0, WIDTH, HEADER_H], fill=color)
    draw.text((28, HEADER_H / 2), title, font=_font(60, "Bold"), fill="#ffffff", anchor="lm")
    if right:
        draw.text((WIDTH - 28, HEADER_H / 2), right, font=_font(28, "Medium"), fill="#ffffff", anchor="rm")


def _footer_credit(draw: ImageDraw.ImageDraw) -> None:
    draw.text((WIDTH - 16, HEIGHT - 10), "出典: 気象庁ホームページのGISデータを加工して作成",
              font=_font(15), fill="#8a8f95", anchor="rd")


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    # optimize=True は作成時間の大半を占めるのに、サイズはほとんど変わらないので使わない
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_map(img: Image.Image, fills: dict[str, str], names: list[str],
              hypo: dict[str, Any], home_area: str) -> bool:
    """左側の地図を描く。震源を地図に描けたら True."""
    lat, lon = _float(hypo.get("latitude")), _float(hypo.get("longitude"))
    has_epi = lat is not None and lon is not None and -90 <= lat <= 90
    extent = _map_extent(names, [(lon, lat)] if has_epi else [])
    zoomed = extent != MAIN_EXTENT
    # 地図は別の画像に描いてから枠の大きさで切り取る（枠の外にはみ出した線を右側に残さない）
    mimg = Image.new("RGB", (WIDTH, HEIGHT), C_SEA)
    mdraw = ImageDraw.Draw(mimg)
    proj = _Projector(extent, MAP_BOX)
    _draw_areas(mdraw, proj, fills, extent, width=2 if zoomed else 1)

    if home_area:
        center = _outline_area(mdraw, proj, home_area, C_TEXT, 4)
        if center:
            cx, cy = center
            mdraw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=C_TEXT, outline="#ffffff", width=3)

    epicenter_on_map = False
    if has_epi and _inside(lon, lat, extent):
        _draw_epicenter(mdraw, *proj(lon, lat))
        epicenter_on_map = True

    # 沖縄・奄美の小さな地図（拡大しているときは、そこに対象地域か震源があるときだけ）
    boxes = _bboxes()
    okinawa_involved = any(
        (b := boxes.get(n)) is not None and _inside((b[0] + b[1]) / 2, (b[2] + b[3]) / 2, INSET_EXTENT)
        for n in names
    ) or (has_epi and _inside(lon, lat, INSET_EXTENT))
    okinawa_on_main = _inside(127.8, 26.4, extent)
    if not zoomed or (okinawa_involved and not okinawa_on_main):
        inset = _Projector(INSET_EXTENT, INSET_BOX)
        mdraw.rectangle(INSET_BOX, fill=C_SEA, outline="#8a8f95", width=2)
        _draw_areas(mdraw, inset, fills, INSET_EXTENT)
        mdraw.text((INSET_BOX[0] + 8, INSET_BOX[1] + 4), "沖縄・奄美", font=_font(18, "Medium"), fill=C_SUB)
        if has_epi and not epicenter_on_map and _inside(lon, lat, INSET_EXTENT):
            _draw_epicenter(mdraw, *inset(lon, lat), r=9)
            epicenter_on_map = True
    img.paste(mimg.crop(MAP_BOX), MAP_BOX[:2])
    return epicenter_on_map


def _hypo_lines(draw: ImageDraw.ImageDraw, hypo: dict[str, Any], max_w: int) -> list[str]:
    name = str(hypo.get("name") or "").strip() or "震源不明"
    return _wrap(draw, name, _font(36, "Bold"), max_w)[:2]


def hypocenter_height(draw: ImageDraw.ImageDraw, hypo: dict[str, Any], max_w: int) -> int:
    """震源の欄の高さ（区切り線から詳細の行まで）."""
    return 16 + 34 + 48 * len(_hypo_lines(draw, hypo, max_w)) + 40


def _draw_hypocenter(draw: ImageDraw.ImageDraw, x: int, y: int, max_w: int,
                     hypo: dict[str, Any], origin: str, on_map: bool, assumed: bool = False) -> int:
    """右下の震源の欄を描き、下端の y を返す.

    assumed: 仮定震源要素（震源・規模を推定できていない速報）。M・深さは仮の値なので出さない。
    """
    draw.line([(x, y), (x + max_w, y)], fill="#dddddd", width=2)
    y += 16
    mag = _float(hypo.get("magnitude"))
    depth = _int(hypo.get("depth"), -1)
    if assumed:
        label = "震源（仮定。震源・規模は推定できていません）"
    else:
        label = "震源" + ("" if on_map else "（地図の範囲外）")
    draw.text((x, y), label, font=_fit_font(draw, label, PANEL_W, 24, 16, "Regular"), fill=C_SUB)
    y += 34
    for line in _hypo_lines(draw, hypo, max_w):
        draw.text((x, y), line, font=_font(36, "Bold"), fill=C_TEXT)
        y += 48
    detail = "　".join(t for t in (
        f"M{mag:g}" if mag is not None and mag >= 0 and not assumed else "",
        (("深さごく浅い" if depth == 0 else f"深さ{depth}km") if depth >= 0 else "") if not assumed else "",
        f"発生 {origin}" if origin else "",
    ) if t)
    if detail:
        draw.text((x, y), detail, font=_font(28), fill=C_TEXT)
        y += 40
    return y


def render_cancel(data: dict[str, Any]) -> bytes:
    issue = _mapping(data.get("issue"))
    img = Image.new("RGB", (WIDTH, HEIGHT), C_BG)
    draw = ImageDraw.Draw(img)
    _header(draw, "緊急地震速報 取消", f"{_hhmmss(issue.get('time'))} 発表".strip(), C_CANCEL)
    draw.text((WIDTH / 2, 380), "先ほどの緊急地震速報は", font=_font(72, "Bold"), fill=C_TEXT, anchor="mm")
    draw.text((WIDTH / 2, 500), "取り消されました", font=_font(96, "Bold"), fill=C_CANCEL, anchor="mm")
    return _to_png(img)


def render(data: dict[str, Any], home_area: str = "") -> bytes:
    """556 の JSON から警告画像（PNG）を作る。どんな形のデータでも例外を出さずに何かを描く."""
    if not isinstance(data, dict):
        data = {}
    if data.get("cancelled") is True:
        return render_cancel(data)

    quake = _mapping(data.get("earthquake"))
    hypo = _mapping(quake.get("hypocenter"))
    issue = _mapping(data.get("issue"))
    raw_areas = data.get("areas")
    areas = [a for a in (raw_areas if isinstance(raw_areas, list) else []) if isinstance(a, dict) and isinstance(a.get("name"), str)]

    fills = {a["name"]: _eew_scale(a)[2] for a in areas}
    home = next((a for a in areas if home_area and a["name"] == home_area), None)
    # 仮定震源要素: 震源・規模を推定できていない速報。震源の位置・M・深さは仮の値なので描かない
    assumed = "仮定震源要素" in str(quake.get("condition") or "")
    map_hypo = {} if assumed else hypo

    img = Image.new("RGB", (WIDTH, HEIGHT), C_BG)
    draw = ImageDraw.Draw(img)

    # 見出し
    serial = str(issue.get("serial") or "").strip()
    right = "　".join(x for x in (f"第{serial}報" if serial else "", f"{_hhmmss(issue.get('time'))} 発表" if _hhmmss(issue.get("time")) else "") if x)
    title = "緊急地震速報（警報）"
    if data.get("test") is True:
        title = "【テスト】" + title
    _header(draw, title, right, C_ALERT)

    # 地図
    epicenter_on_map = _draw_map(img, fills, [a["name"] for a in areas], map_hypo, home_area)

    # 右側
    x, max_w = PANEL_X, WIDTH - PANEL_X - 28
    y = HEADER_H + 26
    if home_area and home is not None:
        s_from, s_to = _int(home.get("scaleFrom")), _int(home.get("scaleTo"))
        short = home_area.split("都")[-1] if home_area.startswith("東京都") else home_area
        draw.text((x, y), f"{short}の予想震度", font=_font(36, "Bold"), fill=C_TEXT)
        y += 58
        _, big, box_color = _eew_scale(home)
        draw.rounded_rectangle([x, y, x + max_w, y + 190], radius=18, fill=box_color, outline="#888888")
        draw.text((x + 26, y + 95), "震度", font=_font(52, "Bold"), fill=_text_color_on(box_color), anchor="lm")
        draw.text((x + max_w - 26, y + 98), big, font=_fit_font(draw, big, max_w - 190, 150, 60, "Black"), fill=_text_color_on(box_color), anchor="rm")
        y += 204
        if s_to != 99 and s_from != s_to and s_from >= 0 and s_from in SCALE_LABEL and s_to in SCALE_LABEL:
            draw.text((x, y), f"（予想の幅 {SCALE_LABEL.get(s_from, '?')}〜{SCALE_LABEL.get(s_to, '?')}）", font=_font(26), fill=C_SUB)
            y += 40
        if 0 <= s_to < 40:
            for line in _wrap(draw, "長周期地震動で対象になった可能性があります（高い階・大きな揺れが長く続く）", _font(24, "Medium"), max_w):
                draw.text((x, y), line, font=_font(24, "Medium"), fill=C_ALERT)
                y += 34
        arrival = _hhmmss(home.get("arrivalTime"))
        kind = str(home.get("kindCode") or "")
        if kind == "11":
            # 主要動について、既に到達と予測
            y += 6
            draw.text((x, y), "揺れはすでに到達しているとみられます", font=_fit_font(draw, "揺れはすでに到達しているとみられます", max_w, 30, 22, "Bold"), fill=C_ALERT)
            y += 56
        elif arrival:
            y += 6
            draw.text((x, y), "揺れの到達予想", font=_font(28, "Medium"), fill=C_SUB)
            draw.text((x + max_w, y - 4), arrival, font=_font(40, "Bold"), fill=C_TEXT, anchor="ra")
            y += 56
    else:
        draw.text((x, y), "強い揺れに警戒", font=_font(52, "Bold"), fill=C_ALERT)
        y += 80
        ranked = sorted(areas, key=lambda a: -_eew_scale(a)[0])[:6]
        for a in ranked:
            label = _eew_scale(a)[1]
            text = f"震度{label}" if label != "不明" else "震度不明"
            tw = draw.textlength(text, font=_font(30, "Bold"))
            name_font = _fit_font(draw, a["name"], int(max_w - tw - 16), 30, 18, "Medium")
            draw.text((x, y), a["name"], font=name_font, fill=C_TEXT)
            draw.text((x + max_w, y), text, font=_font(30, "Bold"), fill=C_TEXT, anchor="ra")
            y += 44
        if home_area:
            draw.text((x, y + 6), f"{home_area}は対象外", font=_font(26), fill=C_SUB)
            y += 44

    # 震源
    hypo_top = min(max(y + 10, 560), HEIGHT - 42 - hypocenter_height(draw, hypo, max_w))
    _draw_hypocenter(draw, x, hypo_top, max_w, hypo, _hhmmss(quake.get("originTime")), epicenter_on_map, assumed)

    _footer_credit(draw)
    return _to_png(img)


# ---- 地震情報（551） ----

# 観測点名が市町村名で始まらないもの（空港など）を、所在する市町村に読み替える
_POINT_ALIASES = {
    "成田国際空港": "成田市",
    "東京国際空港": "大田区",
    "仙台空港": "岩沼市",
    "新千歳空港": "千歳市",
    "関西国際空港": "泉佐野市",
    "中部国際空港": "常滑市",
    "大阪国際空港": "豊中市",
    "鹿児島空港": "霧島市",
}

ISSUE_TYPE_LABEL = {
    "ScalePrompt": "震度速報",
    "Destination": "震源に関する情報",
    "ScaleAndDestination": "震源・震度情報",
    "DetailScale": "各地の震度",
    "Foreign": "遠地地震",
    "Other": "地震情報",
}

TSUNAMI_TEXT = {
    "None": ("この地震による津波の心配はありません", C_TEXT),
    "NonEffective": ("若干の海面変動があるかもしれませんが、被害の心配はありません", C_TEXT),
    "Checking": ("津波の有無を調査中", C_ALERT),
    "Watch": ("津波注意報が出ています", C_ALERT),
    "Warning": ("津波警報などが出ています", C_ALERT),
}


def _observed_color(scale: int) -> str:
    """観測された震度の色。震度1〜3も見分けられるようにしたもの."""
    if scale >= 40:
        return _scale_color(scale)
    if scale >= 30:
        return "#3d8bf2"
    if scale >= 20:
        return "#8cc4ff"
    return "#d4e9ff"


@lru_cache(maxsize=1)
def _cities() -> dict[str, str]:
    with open(CITIES_PATH, encoding="utf-8") as f:
        return json.load(f)


def point_area(point: Any) -> str | None:
    """地震情報の観測点（または区域）が属する細分区域の名前。分からなければ None."""
    if not isinstance(point, dict) or not isinstance(point.get("addr"), str):
        return None
    # 観測点名は長くても十数文字。異常に長い名前で前方一致の照合が重くならないよう切る
    addr = point["addr"].strip()[:32]
    if point.get("isArea") is True:
        return addr if addr in _areas() else None
    cities = _cities()
    candidates = [addr]
    if addr.startswith("東京"):  # 「東京千代田区大手町」→「千代田区」（区の名前に「東京」が付かないものがある）
        candidates.append(addr[2:])
    for alias, city in _POINT_ALIASES.items():
        if addr.startswith(alias):
            candidates.insert(0, city)
    for cand in candidates:
        for n in range(len(cand), 1, -1):
            area = cities.get(cand[:n])
            if area:
                return area
    return None


def area_scales(data: Any) -> dict[str, int]:
    """地震情報の観測点を細分区域ごとにまとめ、区域ごとの最大震度を返す（46 は 45 として扱う）."""
    out: dict[str, int] = {}
    points = _mapping(data).get("points")
    for p in points if isinstance(points, list) else []:
        area = point_area(p)
        scale = _int(p.get("scale")) if isinstance(p, dict) else -1
        if scale == 46:
            scale = 45
        if area and scale in SCALE_LABEL and scale > 0:
            out[area] = max(out.get(area, 0), scale)
    return out


def _hhmm(value: Any) -> str:
    text = str(value or "")
    try:
        return datetime.strptime(text[:16], "%Y/%m/%d %H:%M").strftime("%H:%M")
    except ValueError:
        return ""


def _short_area(name: str) -> str:
    return name.split("都")[-1] if name.startswith("東京都") else name


def render_quake(data: dict[str, Any], home_area: str = "") -> bytes:
    """551（地震情報）の JSON から画像（PNG）を作る。どんな形のデータでも例外を出さずに何かを描く."""
    if not isinstance(data, dict):
        data = {}
    quake = _mapping(data.get("earthquake"))
    hypo = _mapping(quake.get("hypocenter"))
    issue = _mapping(data.get("issue"))
    scales = area_scales(data)
    fills = {n: _observed_color(v) for n, v in scales.items()}

    img = Image.new("RGB", (WIDTH, HEIGHT), C_BG)
    draw = ImageDraw.Draw(img)

    kind = ISSUE_TYPE_LABEL.get(str(issue.get("type") or ""), "地震情報")
    issued = _hhmm(issue.get("time"))
    title = "地震情報" + ("（訂正）" if str(issue.get("correct") or "None") not in ("None", "Unknown") else "")
    if data.get("test") is True:
        title = "【テスト】" + title
    right = "　".join(t for t in (kind, f"{issued} 発表" if issued else "") if t)
    _header(draw, title, right, C_INFO)

    epicenter_on_map = _draw_map(img, fills, list(scales), hypo, home_area)

    x, max_w = PANEL_X, WIDTH - PANEL_X - 28
    y = HEADER_H + 26
    max_scale = _int(quake.get("maxScale"))
    if max_scale == 46:
        max_scale = 45
    if max_scale in SCALE_LABEL and max_scale > 0:
        draw.text((x, y), "最大震度", font=_font(36, "Bold"), fill=C_TEXT)
        y += 58
        color = _observed_color(max_scale)
        draw.rounded_rectangle([x, y, x + max_w, y + 130], radius=18, fill=color, outline="#888888")
        draw.text((x + 26, y + 65), "震度", font=_font(48, "Bold"), fill=_text_color_on(color), anchor="lm")
        label = SCALE_LABEL[max_scale]
        draw.text((x + max_w - 26, y + 68), label, font=_fit_font(draw, label, max_w - 180, 110, 60, "Black"),
                  fill=_text_color_on(color), anchor="rm")
        y += 146
    else:
        draw.text((x, y), "震源に関する情報" if hypo else "地震情報", font=_font(36, "Bold"), fill=C_TEXT)
        y += 58

    tsunami = TSUNAMI_TEXT.get(str(quake.get("domesticTsunami") or ""))
    if tsunami and tsunami[1] == C_ALERT:
        # 津波の注意が必要なときは、音で気づけなくても目に入るよう赤い帯で大きく出す
        draw.rounded_rectangle([x, y, x + max_w, y + 60], radius=12, fill=C_ALERT)
        draw.text((x + max_w / 2, y + 30), tsunami[0], font=_fit_font(draw, tsunami[0], max_w - 24, 34, 20, "Bold"),
                  fill="#ffffff", anchor="mm")
        y += 72
        tsunami = None

    if home_area and scales:
        short = _short_area(home_area)
        if home_area in scales:
            text = f"{short}：震度{SCALE_LABEL[scales[home_area]]}"
            draw.text((x, y), text, font=_fit_font(draw, text, max_w, 40, 24, "Bold"), fill=C_TEXT)
        else:
            draw.text((x, y), f"{short}：震度1以上の観測なし", font=_fit_font(draw, f"{short}：震度1以上の観測なし", max_w, 30, 22, "Medium"), fill=C_SUB)
        y += 56

    # 震度の大きい区域（自宅の地域は上で出しているので除く）。震源の欄（下から約180px）に重ならない数だけ
    t_font = _font(24, "Medium")
    t_lines = _wrap(draw, tsunami[0], t_font, max_w)[:2] if tsunami else []
    hypo_top = HEIGHT - 42 - hypocenter_height(draw, hypo, max_w)
    ranked = sorted(((n, v) for n, v in scales.items() if n != home_area), key=lambda t: -t[1])
    room = hypo_top - 10 - y - 6 - 32 * len(t_lines)
    rows = max(0, min(len(ranked), room // 40))
    if rows < len(ranked):
        rows = max(0, (room - 32) // 40)
    for n, v in ranked[:rows]:
        text = f"震度{SCALE_LABEL[v]}"
        tw = draw.textlength(text, font=_font(28, "Bold"))
        draw.text((x, y), n, font=_fit_font(draw, n, int(max_w - tw - 16), 28, 18, "Medium"), fill=C_TEXT)
        draw.text((x + max_w, y), text, font=_font(28, "Bold"), fill=C_TEXT, anchor="ra")
        y += 40
    if rows < len(ranked):
        draw.text((x, y), f"ほか {len(ranked) - rows} 区域", font=_font(22), fill=C_SUB)
        y += 32

    if t_lines:
        y += 6
        for line in t_lines:
            draw.text((x, y), line, font=t_font, fill=tsunami[1])
            y += 32

    _draw_hypocenter(draw, x, max(y + 10, hypo_top), max_w, hypo, _hhmm(quake.get("time")), epicenter_on_map)
    _footer_credit(draw)
    return _to_png(img)


def preload() -> None:
    """地図・対応表・フォントを読み込んでおく（イベントループの外で呼ぶ）."""
    _areas()
    _bboxes()
    _cities()
    for size, weight in ((60, "Bold"), (36, "Bold"), (28, "Regular")):
        _font(size, weight)


def sample_eew(home_area: str = "", now: datetime | None = None) -> dict[str, Any]:
    """テスト画像用の架空の緊急地震速報（test: true。自宅の地域を対象に含める）.

    now: 画像に出す時刻（HA のタイムゾーンの現在時刻を渡す。省略時はこの機械の現地時刻）。
    """
    now = now or datetime.now()
    t = now.strftime("%Y/%m/%d %H:%M:%S")
    arrival = (now + timedelta(seconds=10)).strftime("%Y/%m/%d %H:%M:%S")
    areas = [
        {"name": "東京都２３区", "pref": "東京", "scaleFrom": 50, "scaleTo": 55, "kindCode": "10", "arrivalTime": arrival},
        {"name": "神奈川県東部", "pref": "神奈川", "scaleFrom": 45, "scaleTo": 50, "kindCode": "10", "arrivalTime": arrival},
        {"name": "千葉県北西部", "pref": "千葉", "scaleFrom": 45, "scaleTo": 50, "kindCode": "10", "arrivalTime": arrival},
        {"name": "埼玉県南部", "pref": "埼玉", "scaleFrom": 40, "scaleTo": 45, "kindCode": "10", "arrivalTime": arrival},
    ]
    if home_area and home_area in _areas() and all(a["name"] != home_area for a in areas):
        areas.insert(0, {"name": home_area, "pref": "", "scaleFrom": 45, "scaleTo": 50, "kindCode": "10", "arrivalTime": arrival})
    return {
        "code": 556,
        "test": True,
        "cancelled": False,
        "issue": {"eventId": "test", "serial": "1", "time": t},
        "earthquake": {
            "originTime": t,
            "arrivalTime": t,
            "hypocenter": {"name": "東京湾（テスト）", "latitude": 35.5, "longitude": 139.8, "depth": 40, "magnitude": 6.5},
        },
        "areas": areas,
    }
