# 運用手順

仕様書3.6・4.4・13章に対応する。

## 1. フェーズ1: 実測チェックリスト

仕様書のフェーズ1は「価格を比較する」としか書いておらず判定基準がない
（[spec-review D-5](spec-review.md)）。以下を判定可能な形にした。
**A-1 と A-2 は構成の可否に関わるので最初に潰すこと。**

### 1.1 帯域の実測（最優先・A-1）

無料枠 1GB/月 が成立するかは、仕様書では検証されていない。対象は**最大10銘柄**
（仕様書の30銘柄から引き下げ済み。[spec-review A-1](spec-review.md) 参照）。

```bash
# 2〜3銘柄で1営業日（できればプレ〜アフター通し）動かす
curl -s .../api/health | jq '.bandwidth, .collector.subscribed_symbols'
```

判定:

N銘柄で1日実測したら `bytes_received_today × (10/N) × 20` で10銘柄・1か月へ外挿する。

| 外挿値 | 判断 |
|---|---|
| < 0.8 GB | 枠内。そのまま進める |
| 0.8〜1.0 GB | 枠ぎりぎり。銘柄を数本減らすか、警告閾値を下げて運用する |
| > 1.0 GB | 超過。**意思決定が必要**: 銘柄数をさらに削減／有料枠／主系をAlpacaへ変更 |

**実測に使う銘柄の選び方が結果を左右する。**高流動性銘柄（大型テック、主要ETF）
だけで測ると過大に、閑散銘柄だけで測ると過小に出る。**実際に監視するつもりの
10銘柄から代表を2〜3本選ぶ**こと。

WebSocketトラフィックが1GB枠に算入されるかは、Tiingoに確認すること。
算入される前提で設計してある。

### 1.2 RESTレートの実測（A-2）

```bash
curl -s .../api/health | jq '.rest_budget'
```

1営業日の `calls_today` が 1000 に対して十分小さいこと、`calls_this_hour` が
50に張り付いていないことを確認する。張り付く場合は
`USSTOCKS_BACKFILL_MIN_GAP_SECONDS` を上げる。

### 1.3 価格の突き合わせ

証券会社の画面と並べ、**同一時刻**で比較する。許容幅は自分で決めること。
参考の目安:

| 項目 | 記録する値 | 目安 |
|---|---|---|
| 最終価格の乖離 | 絶対値・% | 通常取引中に0.1%以内なら実用上問題ない |
| 更新の遅れ | 体感の秒数 | 数秒以内 |
| 出来高 | 同時刻の1分足出来高比 | 統合値とIEX単独では桁が違って当然。**差があること自体は異常ではない** |
| 対象銘柄 | 保有銘柄が全て取得できるか | 取得できない銘柄は `symbols.supported=0` になる |

Tiingoの参照価格は公式SIPの最終約定価格そのものではない（仕様書5.1）。
**一致しないことを前提に、乖離が許容範囲かを判断する**のが目的である。

### 1.4 セッション判定の確認

```sql
SELECT session, COUNT(*) FROM bars_1m
WHERE symbol='AAPL' AND timestamp_utc >= '2026-07-27'
GROUP BY session;
```

プレ／通常／アフターが妥当な比率で出ること。休場日や半日立会日に `regular`
の足が出ていないこと。

## 2. 日常の監視

`/api/health` の `problems` 配列が空でなければ何かある。

| 値 | 意味 | 対応 |
|---|---|---|
| `collector_status_stale` | collectorが60秒以上状態を書いていない | プロセス確認・再起動 |
| `collector_disconnected` | WS未接続 | ログでエラー種別を確認。自動再接続を待つ |
| `bandwidth_budget_high` | 月間受信が閾値超過 | 銘柄数を減らすか枠を見直す |
| `disk_usage_high` | ディスク70%超 | 拡張またはアーカイブ（仕様書10.3） |
| `insufficient_headroom_for_backup` | 空きがDBサイズの2倍未満 | **バックアップが取れなくなる**ので先に対処 |

`collector.data_age_seconds` は最終**約定**からの経過秒数である。市場が閉まって
いれば大きくなるのが正常で、`session` と併せて見る必要がある。閑散銘柄と
「フィードが死んだ」は区別できるようになっている。

### ログ

```bash
# systemd direct runtime（推奨）
journalctl -u usstocks-collector -n 200 --no-pager
journalctl -u usstocks-api -u usstocks-deploy.service --since today

# Compose互換構成の場合
docker compose -f deploy/docker-compose.yml logs --tail=200 collector
```

APIキーやトークンはログフィルタでマスクされる（仕様書4.5）。それでも
ログを外部へ貼る前には目視すること。

## 3. バックアップとリストア

### 日次バックアップ

`deploy/backup/backup.sh` は次を行う。

1. 空き容量がDBサイズの2倍未満なら**実行を拒否する**（`VACUUM INTO` は
   DBとほぼ同サイズの複製を作るため。仕様書10.4はこの前提に触れていない）
2. `VACUUM INTO` で整合した複製を作る（稼働中のファイルを `cp` しない）
3. `PRAGMA integrity_check` で複製を検証する
4. gzip 圧縮して S3 へアップロードする
5. ローカルの古い世代を削除する

systemd direct runtimeでは`usstocks-backup.timer`が日次実行する。手動確認:

```bash
sudo systemctl start usstocks-backup.service
journalctl -u usstocks-backup.service -n 100 --no-pager
```

### リストア試験（四半期に一度を推奨）

仕様書4.4が求める「定期的に別環境へリストアし有効性を確認する」の実施手順。

```bash
deploy/backup/restore.sh \
  s3://your-bucket/usstocks/daily/market-20260728T071000Z.db.gz \
  /tmp/restore-test.db
```

出力される行数・銘柄数・期間・提供元別内訳が想定どおりかを確認する。
検証が終わったら `/tmp/restore-test.db` を削除する。

### 実際の障害復旧

1. 新しいインスタンスを用意し、[deployment.md](deployment.md) の手順で構築する。
2. バックアップをリストアし、`USSTOCKS_DB_PATH` の位置に置く。
3. collector を起動する。最終保存足から現在までを自動で補完する。

**RPOの実効値は24時間より良い**。1分足はTiingoのREST履歴から取り直せるため、
失われるのはライブ集計にしかない情報（`received_at` 等）に限られる
（[spec-review B-9](spec-review.md)）。ただし補完はRESTレート枠を消費するので、
大きなギャップは数時間かかる。

## 4. よくある操作

### 銘柄の追加・削除

画面から検索して追加するのが通常。CLIからも可能:

```bash
sudo -u usstocks /usr/bin/env \
  USSTOCKS_DB_PATH=/var/lib/usstocks/market.db \
  /opt/usstocks/current/venv/bin/python /opt/usstocks/app/scripts/seed.py TSLA
```

削除（購読解除）しても**履歴は消えない**。これは意図的である
（仕様書2.1のデータ蓄積が目的のため）。

### 臨時休場の登録

国民追悼日などは算出できないので手で入れる（[spec-review B-4](spec-review.md)）。

```sql
INSERT INTO market_calendar_overrides (day, kind, reason)
VALUES ('2026-01-09', 'closed', 'national day of mourning');
```

collector を再起動すると反映される。

### 提供元の切り替え（仕様書5.2）

Tiingo の長時間障害時のみ、**明示的に**行う。

```bash
# /etc/usstocks/usstocks.env
USSTOCKS_PRIMARY_SOURCE=alpaca
USSTOCKS_ALPACA_API_KEY=...
USSTOCKS_ALPACA_API_SECRET=...

sudo systemctl restart usstocks-collector
```

collector だけを再起動する。既存の Tiingo データは上書きされず、Alpaca の足は
別行として蓄積される。チャートは `USSTOCKS_SOURCE_PRIORITY` の順で1本を選び、
混在期間は画面に表示される。復旧後は `USSTOCKS_PRIMARY_SOURCE=tiingo` に戻す。

**自動では切り替えない。**IEX単独データは出来高の定義が違うため、無自覚に
混ざると段差の原因になる。

### エクスポート

```bash
curl -sO -J 'https://stocks.example.com/api/export/csv?symbols=AAPL,MSFT&days=90'
```

Parquetを使う場合はrelease venvに`.[parquet]` extraを含めて構築する必要がある。
1GB機ではpyarrowのメモリ消費に注意し、期間を区切って取得する
（[spec-review D-1](spec-review.md)）。

## 5. 定期作業

| 頻度 | 作業 |
|---|---|
| 毎日 | `/api/health` の `problems` を確認（画面下部にも要約が出る） |
| 毎週 | ディスク使用率、月間受信バイト数の推移 |
| 毎月 | AWS請求、Tiingoの利用量 |
| 四半期 | リストア試験、依存パッケージ更新（`pip list --outdated`）、APIキーのローテーション |
| 随時 | 提供元の利用規約・料金の変更確認（仕様書12） |

## 6. トラブルシューティング

| 症状 | 確認する場所 |
|---|---|
| 画面が更新されない | 画面右上の接続表示 → `/api/health` の `collector` → collectorログ |
| 数分でログイン画面に戻る | Cloudflare Access のセッション期間設定 |
| チャートが空 | 対象期間に足があるか。`symbols.supported` が0でないか |
| 出来高が0の足がある | 約定なしの分は足自体が作られない設計。0出来高の足が出るなら提供元データ側の問題 |
| 起動時に `auth_mode=disabled is only allowed on a loopback` | 意図せず認証を切ったまま公開アドレスにバインドしている。設定ミスの検出であり正常な挙動 |
| `REST budget exhausted` がログに出続ける | 再接続が多すぎる。`USSTOCKS_BACKFILL_MIN_GAP_SECONDS` を上げ、切断原因を調べる |
