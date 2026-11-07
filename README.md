# P2P地震情報 WebSocket（Home Assistant カスタム統合）

> **個人用・無保証です。** 緊急地震速報を受け取るための唯一の手段として使わないでください。
> 公式の緊急速報（携帯電話の緊急速報メール、テレビ・ラジオ）を必ず併用してください。
> Issue・Pull Request には対応しません。

[P2P地震情報](https://www.p2pquake.net/) の WebSocket API（`wss://api.p2pquake.net/v2/ws`）に常時接続し、
**緊急地震速報（警報, code 556）を受信したら、受信した JSON をそのまま `p2pquake_eew` イベントとして発火**します。
地震情報（揺れた後の震度・震源の情報, code 551）は `p2pquake_quake` イベントとして発火します。

この統合は「受信して HA のイベントにする」だけです。地域の判定や照明などの操作はしません。
それらは自分のオートメーションで行ってください。

## できること

- WebSocket への常時接続と、切断時の自動再接続
  - 待ち時間は1秒から最大60秒まで延ばす（±20%のゆらぎ付き）
  - 接続できてもすぐ切られる場合は待ち時間を延ばし続け、データを受け取れた・60秒以上つながった接続のあとで初期値に戻す
  - 障害が続く間、ログの WARNING は最初の1回だけ
- 30秒ごとの ping で切れた接続を検出し、20分間データ（ping/pong 以外）が届かない場合も接続し直す
  （本番ではピア分布 code 555 が数分おきに届くため、20分の無音は配信停止とみなす）
- 同じ情報の再配信を `id`／`_id` で取り除く（公式仕様で推奨されている処理）
  - 対象は 556 と 551 だけ（揺れた報告 561 などが大量に届いても記録が押し出されない）。記録はオプション変更での読み込み直しをまたいで残る
- JSON API（`/v2/history`）による予備の受信（本番のみ）
  - 地震時に WebSocket の配信が数分遅れた例があったための予備。WebSocket と同じ重複除去・同じ処理を通り、先に届いた方だけを発火する（画像も作る）
  - 556 は10秒ごと（速報が無ければ応答は2バイト）。551 は通常60秒ごと、新しい 551・556 を受け取ってから15分間は15秒ごと
  - 古い情報は発火しない（統合の起動より前のもの、556 は10分・551 は30分より前のもの）。
    ただし HA の起動中は、停止中・起動中に出た3分以内の緊急地震速報を発火する（起動完了の10秒後）。
    オプション変更での読み込み直しでは、その隙に出た情報も拾う
  - REST で発火を見送った情報（古い・時刻が読めない）でも、WebSocket で届けば従来どおり発火する
  - 地震情報は、表示済みの報より古い報を REST で後から発火しない（表示が古い報に戻らない）
  - HA の再起動で重複除去の記録は消えるため、再起動の前に発火した3分以内の緊急地震速報がもう一度発火することがある。
    オートメーション側で `issue.eventId` などを永続的に記録して重複を除いてください
  - HTTP 429・5xx のときだけ待ちを伸ばす（最大60秒、Retry-After に従う）。タイムアウトなどでは周期を保つ
  - 1分あたり最大10回（公式の上限は60回/分/IP）
- 556 を受信したら `p2pquake_eew` イベントを発火
  - HA の起動中（オートメーションの準備ができる前）に受信したものは保留し、起動完了の10秒後に発火する（受信から3分以内のもの）
- 551（地震情報）を受信したら `p2pquake_quake` イベントを発火（HA の起動中に受信したものは発火しない）
- エンティティ（センサーの状態が変わるのは接続・切断時と速報受信時だけ）
  - `binary_sensor.p2pquake_ws_connected`：接続中か
  - `binary_sensor.p2pquake_ws_rest`：JSON API で緊急地震速報を取得できているか（直近60秒以内に成功していれば on。本番のみ）
  - `sensor.p2pquake_ws_last_eew`：最後に緊急地震速報を受信した時刻と概要（再起動後も保持）
- 画像（オプション、既定でオン。PNG、1280×800）
  - `image.p2pquake_ws_eew_map`：556 を受信するたびに、地図（対象地域を予想震度で色分け、震源）と文字の画像を作る
    - 「自宅の地域」を選ぶと、その地域の予想震度と揺れの到達予想時刻を大きく表示する（対象外ならそう表示する）
  - `image.p2pquake_ws_quake_map`：551 を受信するたびに、地図（観測された震度で細分区域を色分け、震源）と、
    最大震度・自宅の地域の震度・震度の大きい地域・津波の有無の画像を作る
  - 地図は対象地域と震源に合わせて拡大する
  - 画像はイベントを発火した後で作る（作成に失敗・遅延してもイベントには影響しない。作成は0.1秒程度）
  - 地図などのファイルを読み込めないときは画像の機能だけを止め、「設定 → 修理」に警告を出す（イベントは発火し続ける）
  - 画像をオフにすると、画像とテストボタンのエンティティは登録から外れる
  - `button.p2pquake_ws_test_image`：押すと【テスト】と付いた架空の緊急地震速報の画像を `image.p2pquake_ws_eew_map` に作る。
    イベントは発火しないので、オートメーションは動かない（画面付き端末での表示確認用）。
    本物の緊急地震速報の受信から10分間は、本物の画像を残すため押しても作らない
- 接続先を本番／サンドボックス（2023年の情報を約30秒ごとに再送する動作確認用）から選べる
  - サンドボックス接続中は「設定 → 修理」に警告を出す（本物の速報が届かないため）

外部ライブラリには依存しません（HA 同梱の aiohttp と Pillow を使用）。

### データベース（recorder）への記録について

- 画像のエンティティは、HA の仕組みで5分ごとにアクセス用のトークンが変わり、そのたびに状態が記録されます（2つで1日約576件）。
  気になる場合は `configuration.yaml` で記録から外してください。
- `p2pquake_quake` イベントには観測点がすべて入るため、大きな地震では1件が数百KBになります。

```yaml
recorder:
  exclude:
    entity_globs:
      - image.p2pquake_ws_*
    event_types:
      - p2pquake_quake
```

## イベントの中身

`p2pquake_eew` のデータは、[公式仕様](https://www.p2pquake.net/develop/json_api_v2/) の EEW（code 556）そのままです。
次のキーを追加しています。

| キー | 内容 |
|---|---|
| `_received_at` | HA が受信した時刻（UTC, ISO 8601） |
| `_message_id` | 情報の識別子（WebSocket では `_id` で届く） |
| `_endpoint` | `production` または `sandbox` |
| `_source` | 受信した経路。`ws`（WebSocket）または `rest`（JSON API） |
| `_p2p_delay` | P2P地震情報が情報を受け付けた時刻（`time`）から HA が受信するまでの秒数（`time` が読めないときは無い） |

JSON API で届いた情報にも `_id` を付けています（WebSocket と同じ形）。
同じ地震の古い報が後から届いた場合（経路の違いで順番が入れ替わった場合）、イベントは発火しますが、
`sensor.p2pquake_ws_last_eew` と画像は新しい報のままにします。

次のものも、そのまま流れてきます。オートメーション側で除外してください。

- `test: true`：訓練・試験配信
- `cancelled: true`：取消報（`earthquake` と `areas` が null）
- `_endpoint: sandbox`：サンドボックスの再送データ（`test` は false のまま届く）

`p2pquake_quake` のデータは、公式仕様の地震情報（code 551）そのままに、上のキーと次のキーを追加しています。

| キー | 内容 |
|---|---|
| `_area_scales` | 観測点を細分区域ごとにまとめた最大震度（例: `{"東京都２３区": 20}`。震度の値は 10=1, 20=2, 30=3, 40=4, 45=5弱, 50=5強, 55=6弱, 60=6強, 70=7） |

1つの地震について、震度速報・震源に関する情報・各地の震度に関する情報など、複数回発火します。

同じ地震について、緊急地震速報は続報（`issue.serial` が増える）ごとにイベントが発火します。
1回だけ動かしたい場合は、**地域の条件を満たした後で** `earthquake.originTime` などを記録して重複を除いてください
（第1報では対象外でも、続報で対象地域に加わることがあるため）。

## オートメーションの例

地域名は気象庁の緊急地震速報の細分区域名です。`東京都２３区` の部分を自分の地域に置き換えてください。

```yaml
triggers:
  - trigger: event
    event_type: p2pquake_eew
conditions:
  - condition: template
    value_template: >
      {{ trigger.event.data.get('_endpoint') == 'production'
         and not trigger.event.data.get('cancelled', false)
         and not trigger.event.data.get('test', false)
         and (trigger.event.data.get('areas') or [])
             | selectattr('name', 'search', '東京都２３区') | list | count > 0 }}
actions:
  - action: light.turn_on
    target:
      entity_id: light.example
```

### 画面付き端末に画像を出す例（TECHO5 の Echo Show）

統合のオプションで「自宅の地域」を選んでおきます。画像は少し遅れて更新されるので、文字の通知を先に出し、
画像の更新を待ってから画像を出します。ESPHome の機器名（`living`）は自分のものに置き換えてください。
TECHO5 が画像エンティティを取得するには、事前に `esphome.<機器名>_home_assistant` で HA の URL とトークンを設定しておく必要があります。
続報で表示を更新できるよう、`mode: restart` にします（既定の `single` では、画像を待っている間に届いた続報が捨てられます）。

```yaml
mode: restart
actions:
  - action: esphome.living_notify
    data: {title: 緊急地震速報, message: 強い揺れに警戒, icon: "mdi:alert", chime: true, speak: "", tag: eew, seconds: 0}
  # 画像がこの速報の分に更新されるまで待つ（先に更新されていればすぐ進む）
  - wait_template: >
      {{ states.image.p2pquake_ws_eew_map.last_changed >= as_datetime(trigger.event.data._received_at) }}
    timeout: 5
    continue_on_timeout: false
  - action: esphome.living_notify_picture
    data: {title: "", message: "", picture: image.p2pquake_ws_eew_map, chime: false, speak: "", sound: "", tag: eew, seconds: 0}
```

地震情報を出す例（東京都２３区で震度1以上、または全国で震度4以上のとき。10分で消える）:

```yaml
mode: restart
triggers:
  - trigger: event
    event_type: p2pquake_quake
    event_data:
      _endpoint: production
conditions:
  - condition: template
    value_template: >
      {% set s = trigger.event.data.get('_area_scales') or {} %}
      {{ (trigger.event.data.get('earthquake') or {}).get('maxScale', -1) >= 40
         or s.get('東京都２３区', 0) >= 10 }}
actions:
  - wait_template: >
      {{ states.image.p2pquake_ws_quake_map.last_changed >= as_datetime(trigger.event.data._received_at) }}
    timeout: 10
    continue_on_timeout: false
  - action: esphome.living_notify_picture
    data: {title: "", message: "", picture: image.p2pquake_ws_quake_map, chime: false, speak: "", sound: "", tag: quake, seconds: 600}
```

## インストール

Home Assistant 2025.2 以降が必要です。

HACS → カスタムリポジトリに `https://github.com/Rito0312/ha-p2pquake-ws`（カテゴリ: Integration）を追加 → ダウンロード → HA を再起動 →
設定 → デバイスとサービス → 統合を追加 →「P2P地震情報 WebSocket」。

## 注意

- P2P地震情報の配信は無保証で、サーバーは冗長化されていません。障害中に出た情報は再配信されません。
- WebSocket は IP アドレスあたり2接続までです。JSON API（`/v2/history`）は60リクエスト/分/IPまでです。
- WebSocket と JSON API は同じサーバーです。サーバー自体が止まればどちらも届きません。
- データ提供: [P2P地震情報](https://www.p2pquake.net/)

## 開発

```sh
pip install -r requirements_test.txt
python -m pytest tests/test_client.py tests/test_fetch_http.py -p no:homeassistant -p no:socket  # 受信部分（ローカルのサーバーを使う）
python -m pytest tests/test_integration.py tests/test_poller.py tests/test_render.py  # pytest-homeassistant-custom-component が必要
```

2つのテストは別々に実行してください（HA のテスト環境が通信を禁止するため、ローカルのサーバーを使うテストが失敗します）。

## ライセンス

コードは MIT です。同梱のデータは次のとおりです。

- `assets/areas.json`：気象庁「地震情報／細分区域」の GIS データを簡略化・加工して作成。
  出典：[気象庁ホームページ](https://www.data.jma.go.jp/developer/gis.html)（[利用規約](https://www.jma.go.jp/jma/kishou/info/coment.html)）
- `assets/cities.json`：気象庁「市町村等（地震津波関係）」の GIS データの各市町村を、最も重なる細分区域に割り当てて作成。
  出典は同上
- 画像の右下に「出典: 気象庁ホームページのGISデータを加工して作成」と表示しています
- `assets/NotoSansJP.ttf`：[Noto Sans JP](https://fonts.google.com/noto/specimen/Noto+Sans+JP)（SIL Open Font License 1.1、`assets/OFL.txt`）
