"""render.py の単体テスト（HA 不要。Pillow が必要）."""
import copy
import importlib.util
import io
import pathlib

import pytest
from PIL import Image

SPEC = importlib.util.spec_from_file_location(
    "p2p_render",
    pathlib.Path(__file__).parents[1] / "custom_components/p2pquake_ws/render.py",
)
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)

# 2026-10-16 の千島列島の緊急地震速報（第2報）を縮めたもの
EEW = {
    "code": 556,
    "cancelled": False,
    "issue": {"eventId": "20261016123810", "serial": "2", "time": "2026/10/16 12:40:41"},
    "earthquake": {
        "originTime": "2026/10/16 12:36:27",
        "arrivalTime": "2026/10/16 12:38:10",
        "hypocenter": {"name": "千島列島", "latitude": 48.2, "longitude": 153.4, "depth": 140, "magnitude": 9.9},
    },
    "areas": [
        {"name": "神奈川県東部", "pref": "東京", "scaleFrom": 0, "scaleTo": 40, "kindCode": "10", "arrivalTime": "2026/10/16 12:43:02"},
        {"name": "石狩地方中部", "pref": "北海道道央", "scaleFrom": 55, "scaleTo": 70, "kindCode": "10", "arrivalTime": "2026/10/16 12:39:00"},
    ],
}


def _size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as img:
        assert img.format == "PNG"
        return img.size


def test_area_names_cover_the_warning_areas():
    names = render.area_names()
    assert len(names) >= 190
    for n in ("神奈川県東部", "東京都２３区", "石狩地方中部", "沖縄県本島北部"):
        assert n in names


@pytest.mark.parametrize("home", ["神奈川県東部", "", "存在しない地域"])
def test_render_eew(home):
    assert _size(render.render(EEW, home)) == (render.WIDTH, render.HEIGHT)


def test_render_cancel_and_test_flag():
    cancel = {"code": 556, "cancelled": True, "earthquake": None, "areas": None,
              "issue": {"eventId": "20261016123810", "serial": "2", "time": "2026/10/16 12:48:50"}}
    assert _size(render.render(cancel, "神奈川県東部")) == (render.WIDTH, render.HEIGHT)
    test = copy.deepcopy(EEW)
    test["test"] = True
    assert _size(render.render(test, "神奈川県東部")) == (render.WIDTH, render.HEIGHT)


@pytest.mark.parametrize("data", [
    None, {}, [], "x",
    {"areas": "神奈川県東部", "earthquake": [1], "issue": "x"},
    {"areas": [{"name": "神奈川県東部", "scaleTo": "x", "scaleFrom": None, "arrivalTime": 5}, {"name": 3}, "x"]},
    {"areas": [{"name": "神奈川県東部", "scaleFrom": 55, "scaleTo": 99}],
     "earthquake": {"originTime": "bad", "hypocenter": {"latitude": "x", "longitude": None, "magnitude": -1, "depth": -1}}},
    {"earthquake": {"hypocenter": {"name": "沖縄本島近海", "latitude": 26.5, "longitude": 127.8}}},
])
def test_render_never_raises_on_odd_data(data):
    assert _size(render.render(data, "神奈川県東部")) == (render.WIDTH, render.HEIGHT)


def test_map_zooms_to_the_warning_areas():
    # 関東だけなら関東を拡大し、日本全体なら全体、沖縄だけなら沖縄を拡大する
    kanto = render._map_extent(["神奈川県東部", "東京都２３区"], [(139.8, 35.5)])
    assert kanto != render.MAIN_EXTENT and render._inside(139.4, 35.7, kanto)
    assert render._map_extent(["石狩地方中部", "福岡県福岡"], []) == render.MAIN_EXTENT
    oki = render._map_extent(["沖縄県本島北部"], [(127.9, 26.3)])
    assert render._inside(127.8, 26.4, oki) and not render._inside(139.4, 35.7, oki)
    assert render._map_extent([], []) == render.MAIN_EXTENT
    assert render._map_extent(["存在しない地域"], [(0.0, 0.0)]) == render.MAIN_EXTENT


QUAKE = {
    "code": 551,
    "issue": {"source": "気象庁", "time": "2026/10/29 00:16:00", "type": "DetailScale", "correct": "None"},
    "earthquake": {
        "time": "2026/10/29 00:07:00", "maxScale": 45, "domesticTsunami": "None", "foreignTsunami": "Unknown",
        "hypocenter": {"name": "千葉県北東部", "latitude": 35.7, "longitude": 140.7, "depth": 50, "magnitude": 5.4},
    },
    "points": [
        {"pref": "千葉県", "addr": "旭市南堀之内", "isArea": False, "scale": 45},
        {"pref": "東京都", "addr": "横浜中区山手町", "isArea": False, "scale": 20},
        {"pref": "東京都", "addr": "東京千代田区大手町", "isArea": False, "scale": 20},
        {"pref": "東京都", "addr": "東京国際空港", "isArea": False, "scale": 30},
        {"pref": "千葉県", "addr": "成田国際空港", "isArea": False, "scale": 46},
        {"pref": "茨城県", "addr": "存在しない観測点", "isArea": False, "scale": 10},
    ],
}


@pytest.mark.parametrize("point,area", [
    ({"addr": "横浜中区山手町", "isArea": False}, "神奈川県東部"),
    ({"addr": "小田原市荻窪", "isArea": False}, "神奈川県西部"),
    ({"addr": "東京千代田区大手町", "isArea": False}, "東京都２３区"),
    ({"addr": "東京国際空港", "isArea": False}, "東京都２３区"),
    ({"addr": "茨城古河市長谷町", "isArea": False}, "茨城県南部"),
    ({"addr": "神奈川県東部", "isArea": True}, "神奈川県東部"),
    ({"addr": "存在しない区域", "isArea": True}, None),
    ({"addr": "存在しない観測点", "isArea": False}, None),
    ({"addr": 5}, None),
    (None, None),
])
def test_point_area(point, area):
    assert render.point_area(point) == area


def test_area_scales():
    scales = render.area_scales(QUAKE)
    assert scales["神奈川県東部"] == 20
    assert scales["東京都２３区"] == 30  # 同じ区域の観測点は大きい方
    assert scales["千葉県北西部"] == 45  # 46（5弱以上と推定）は 5弱 として扱う
    assert "茨城県南部" not in scales
    assert render.area_scales(None) == {} and render.area_scales({"points": "x"}) == {}


@pytest.mark.parametrize("home", ["神奈川県東部", "", "沖縄県本島北部"])
def test_render_quake(home):
    assert _size(render.render_quake(QUAKE, home)) == (render.WIDTH, render.HEIGHT)


@pytest.mark.parametrize("data", [
    None, {}, [], "x",
    {"earthquake": {"hypocenter": {"name": "バヌアツ諸島", "latitude": -17, "longitude": 168, "magnitude": 7.2, "depth": -1},
                    "maxScale": -1, "domesticTsunami": "Checking"}, "issue": {"type": "Foreign"}},
    {"earthquake": {"maxScale": 30, "hypocenter": {"latitude": -200, "longitude": -200}},
     "points": [{"addr": "東京都２３区", "isArea": True, "scale": 30}], "issue": {"type": "ScalePrompt", "correct": "ScaleOnly"}},
    {"earthquake": "x", "points": [None, 1, {"addr": None}], "issue": [], "test": True},
    {"points": [{"addr": f"横浜中区{i}", "isArea": False, "scale": 10} for i in range(3)]
     + [{"addr": n, "isArea": True, "scale": 70} for n in list(render._areas())[:40]]},
])
def test_render_quake_never_raises_on_odd_data(data):
    assert _size(render.render_quake(data, "神奈川県東部")) == (render.WIDTH, render.HEIGHT)


def test_sample_eew_is_a_test_and_includes_home():
    data = render.sample_eew("神奈川県東部")
    assert data["test"] is True and data["code"] == 556
    assert any(a["name"] == "神奈川県東部" for a in data["areas"])
    assert _size(render.render(data, "神奈川県東部")) == (render.WIDTH, render.HEIGHT)
    render.preload()


@pytest.mark.parametrize("area,value,label", [
    ({"scaleFrom": 45, "scaleTo": 99}, 45, "5弱以上"),  # 99 は「〜程度以上」。色・並びは下限で決める
    ({"scaleFrom": 40, "scaleTo": 50}, 50, "5強"),
    ({"scaleFrom": 40, "scaleTo": -1}, 40, "4"),
    ({"scaleFrom": -1, "scaleTo": -1}, -1, "不明"),
    ({}, -1, "不明"),
])
def test_eew_scale(area, value, label):
    v, text, color = render._eew_scale(area)
    assert (v, text) == (value, label)
    if value == 45:
        assert color == render._scale_color(45) != render._scale_color(70)
    if value == -1:
        assert color == render.C_UNKNOWN


def test_area_names_order_and_exclusions():
    names = render.area_names()
    assert names[0] == "石狩地方北部"  # 気象庁の区域の順（北海道から）
    assert "うるま市・金武町境界部地先の埋立地" not in names
    assert len(names) == len(set(names))


def test_render_assumed_hypocenter_and_arrived():
    data = copy.deepcopy(EEW)
    data["earthquake"]["condition"] = "仮定震源要素"
    data["areas"][0].update(kindCode="11", arrivalTime=None, scaleFrom=50, scaleTo=99)
    assert _size(render.render(data, "神奈川県東部")) == (render.WIDTH, render.HEIGHT)
    assert _size(render.render(data, "")) == (render.WIDTH, render.HEIGHT)


@pytest.mark.parametrize("tsunami", ["Warning", "Watch", "Checking", "None", "NonEffective", "Unknown", None])
def test_render_quake_tsunami_and_long_name(tsunami):
    data = copy.deepcopy(QUAKE)
    data["earthquake"]["domesticTsunami"] = tsunami
    data["earthquake"]["hypocenter"]["name"] = "とても長い震央地名の例としての南太平洋の海域"
    assert _size(render.render_quake(data, "神奈川県東部")) == (render.WIDTH, render.HEIGHT)


def test_hypocenter_fits_above_credit():
    from PIL import Image as _I, ImageDraw as _D
    draw = _D.Draw(_I.new("RGB", (10, 10)))
    long = {"name": "とても長い震央地名の例としての南太平洋の海域"}
    assert render.hypocenter_height(draw, long, render.PANEL_W) > render.hypocenter_height(draw, {"name": "東京湾"}, render.PANEL_W)


def test_point_area_long_addr_is_fast():
    import time
    t = time.monotonic()
    render.area_scales({"points": [{"addr": "あ" * 2000, "isArea": False, "scale": 10}] * 2000})
    assert time.monotonic() - t < 1.0


def test_sample_eew_uses_given_time():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=9)))
    data = render.sample_eew("", now)
    assert data["issue"]["time"] == "2026/01/02 03:04:05"
    assert data["areas"][0]["arrivalTime"] == "2026/01/02 03:04:15"
