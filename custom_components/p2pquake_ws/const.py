"""定数."""
DOMAIN = "p2pquake_ws"

CONF_ENDPOINT = "endpoint"
ENDPOINT_PRODUCTION = "production"
ENDPOINT_SANDBOX = "sandbox"
ENDPOINT_URLS = {
    ENDPOINT_PRODUCTION: "wss://api.p2pquake.net/v2/ws",
    ENDPOINT_SANDBOX: "wss://api-realtime-sandbox.p2pquake.net/v2/ws",
}

# 緊急地震速報（警報）を受信したときに発火する HA イベント
EVENT_EEW = "p2pquake_eew"
CODE_EEW = 556

# 地震情報（震度・震源の情報, code 551）を受信したときに発火する HA イベント
EVENT_QUAKE = "p2pquake_quake"
CODE_QUAKE = 551

SIGNAL_UPDATE = f"{DOMAIN}_update"
# JSON API（予備の受信）の取得状態が変わったかもしれないとき
SIGNAL_REST = f"{DOMAIN}_rest"

# 警告画像（オプション）
CONF_IMAGE = "image"
CONF_HOME_AREA = "home_area"
DEFAULT_IMAGE = True
