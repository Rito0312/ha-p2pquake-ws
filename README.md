# P2P地震情報 WebSocket（Home Assistant カスタム統合）

> **個人用・無保証です。** 緊急地震速報を受け取るための唯一の手段として使わないでください。
> 公式の緊急速報（携帯電話の緊急速報メール、テレビ・ラジオ）を必ず併用してください。
> Issue・Pull Request には対応しません。

[P2P地震情報](https://www.p2pquake.net/) の WebSocket API（`wss://api.p2pquake.net/v2/ws`）に常時接続し、
**緊急地震速報（警報, code 556）を受信したら、受信した JSON をそのまま `p2pquake_eew` イベントとして発火**します。

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
- 556 を受信したら `p2pquake_eew` イベントを発火
  - HA の起動中（オートメーションの準備ができる前）に受信したものは保留し、起動完了の10秒後に発火する（受信から3分以内のもの）
- エンティティ（状態が変わるのは接続・切断時と速報受信時だけ。普段はデータベースに書き込まない）
  - `binary_sensor.p2pquake_ws_connected`：接続中か
  - `sensor.p2pquake_ws_last_eew`：最後に緊急地震速報を受信した時刻と概要（再起動後も保持）
- 接続先を本番／サンドボックス（2023年の情報を約30秒ごとに再送する動作確認用）から選べる
  - サンドボックス接続中は「設定 → 修理」に警告を出す（本物の速報が届かないため）

外部ライブラリには依存しません（HA 同梱の aiohttp を使用）。

## イベントの中身

`p2pquake_eew` のデータは、[公式仕様](https://www.p2pquake.net/develop/json_api_v2/) の EEW（code 556）そのままです。
次のキーを追加しています。

| キー | 内容 |
|---|---|
| `_received_at` | HA が受信した時刻（UTC, ISO 8601） |
| `_message_id` | 情報の識別子（WebSocket では `_id` で届く） |
| `_endpoint` | `production` または `sandbox` |

次のものも、そのまま流れてきます。オートメーション側で除外してください。

- `test: true`：訓練・試験配信
- `cancelled: true`：取消報（`earthquake` と `areas` が null）
- `_endpoint: sandbox`：サンドボックスの再送データ（`test` は false のまま届く）

同じ地震について、続報（`issue.serial` が増える）ごとにイベントが発火します。
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

## インストール

Home Assistant 2025.2 以降が必要です。

HACS → カスタムリポジトリに `https://github.com/Rito0312/ha-p2pquake-ws`（カテゴリ: Integration）を追加 → ダウンロード → HA を再起動 →
設定 → デバイスとサービス → 統合を追加 →「P2P地震情報 WebSocket」。

## 注意

- P2P地震情報の配信は無保証で、サーバーは冗長化されていません。障害中に出た情報は再配信されません。
- WebSocket は IP アドレスあたり2接続までです。
- データ提供: [P2P地震情報](https://www.p2pquake.net/)

## 開発

```sh
pip install -r requirements_test.txt
python -m pytest tests/test_client.py                      # 受信部分（HA不要）
python -m pytest tests/test_integration.py                 # pytest-homeassistant-custom-component が必要
```

2つのテストは別々に実行してください（HA のテスト環境が通信を禁止するため、受信部分のテストが失敗します）。

## ライセンス

MIT
