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

SIGNAL_UPDATE = f"{DOMAIN}_update"
