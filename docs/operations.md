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

### 1.2.5 足の密度の実測（A-5・最重要）

**価格が正確でも、足が飛び飛びならチャートとして使えない。**これは精度ではなく
カバレッジの問題で、IEX単独フィードの構造的な性質である
（[spec-review A-5](spec-review.md)）。

```sql
-- 通常取引時間に、実際に足がある分の割合
SELECT
  DATE(timestamp_utc)                       AS day,
  COUNT(*)                                  AS bars,
  ROUND(COUNT(*) * 100.0 / 390, 1)          AS pct_of_session
FROM bars_1m
WHERE symbol = 'AAPL' AND session = 'regular'
GROUP BY day ORDER BY day DESC LIMIT 5;
```

通常取引は 390 分/日なので、`bars` が 390 に近いほど連続している。

| 被覆率 | 判断 |
|---|---|
| 80%以上 | 実用的。無料枠を継続してよい |
| 50〜80% | 銘柄による。流動性の高い銘柄だけに絞れば使える |
| 50%未満 | **統合気配（SIP）への移行を検討する。** Alpaca 有料枠で `USSTOCKS_ALPACA_FEED=sip` |

**計測は必ず通常取引時間で行うこと。**時間外は IEX の板が最も薄く、被覆率が実態より
大幅に低く出る。

移行する場合、**帯域の前提が完全に変わる**（1.1 の 1GB/月 は成立しない）。
`USSTOCKS_MONTHLY_BANDWIDTH_BYTES` の見直しと、ディスク増加の再試算が必要になる。

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
journalctl -u usstocks-news-corpus.service --since today
journalctl -u usstocks-event-study.service --since today

# Compose互換構成の場合
docker compose -f deploy/docker-compose.yml logs --tail=200 collector
```

APIキーやトークンはログフィルタでマスクされる（仕様書4.5）。それでも
ログを外部へ貼る前には目視すること。

REST fetchが成功しても3回連続で0本だった銘柄は、`symbols.supported=0`とnoteが設定され、
ウォッチリストに警告が出る。有効barが戻れば自動解除される。通常16:45 ET以降
（短縮取引日は通常終了45分後）はproviderの既知の配信終了なので、REST pollを行わず
このカウンタも増えない。

### 日足コーパス

通常は`usstocks-corpus.timer`に任せる。特定銘柄を分析へ先行投入する場合だけ、
共有REST予算と安全時間帯を維持したまま対象を明示する。

```bash
sudo -u usstocks /bin/bash -lc \
  'set -a; source /etc/usstocks/usstocks.env; set +a; \
  USSTOCKS_DB_PATH=/var/lib/usstocks/market.db \
  USSTOCKS_CORPUS_LOCAL_DIR=/var/lib/usstocks/corpus \
  USSTOCKS_CORPUS_UNIVERSE_PATH=/opt/usstocks/current/data/universe.csv \
  /opt/usstocks/current/venv/bin/python -m usstocks.corpus.daily \
  --max-symbols 3 --max-new-symbols 3 --symbols MU,WDC,STX'
```

`--symbols`は`data/universe.csv`内だけを許し、重複を除いて指定順に処理する。
通常時間外に緊急実行するときだけ`--force`を追加する。MU分析ではMU本体に加え、
同subsectorのWDC／STXを参考benchmark用に取得する。3社構成なので対象を除くpeerは
最大2社であり、`analysis_min_peers=3`を満たす正式abnormal returnにはならない。

### Notionニュースコーパス

```bash
systemctl list-timers usstocks-news-corpus.timer
sudo systemctl start usstocks-news-corpus.service
journalctl -u usstocks-news-corpus.service -n 100 --no-pager
```

credentialは`/teiten/notion-token`と`/teiten/notion-db-id`をSSMから復号するが、
値はログへ出さない。成功時はページ数・partition数・upload数だけを記録する。
ローカル状態は`/var/lib/usstocks/corpus/news-state.json`、S3は
`corpus/news/date=YYYY-MM-DD/part.parquet`。2回目の実行で変更がなければupload数0が正常。

#### 取り込みが止まったとき（`--check`）

定期実行は規約外の値を1ページ見つけた時点で中断する（`CorpusError`）。
どのページが何件おかしいのかを知るには`--check`を使う。**Parquetを書かず、
S3も触らない**ので本番でそのまま実行してよい。

`usstocks-news-corpus.service` と同じ起動方法（`python -m`）に、credentialを
渡すため`/etc/usstocks/usstocks.env`を読み込んで実行する。**unitと同じ
`WorkingDirectory=/var/lib/usstocks` へ必ず`cd`すること** — 設定は
`env_file=(".env",)`をCWD相対で探すため、別の場所から起動すると無関係な
`.env`を掴み、読めなければ`PermissionError: '.env'`で落ちる。

```bash
sudo bash -c 'cd /var/lib/usstocks && set -a; . /etc/usstocks/usstocks.env; set +a; \
  USSTOCKS_CORPUS_LOCAL_DIR=/var/lib/usstocks/corpus \
  runuser -u usstocks -- /opt/usstocks/current/venv/bin/python \
    -m usstocks.corpus.news --check'
```

事前に、その revision が配備済みであることを確認する（pull agentは
`main`へのpushを検知して更新する）。

```bash
readlink -f /opt/usstocks/current
git -C /opt/usstocks/app rev-parse --short HEAD
```

出力は次の3つ。終了コードは規約外が1件でもあれば1、無ければ0。

| 出力 | 意味 |
|---|---|
| `rejected <page_id> <url>: <理由>` | 同期を止めるページ。URLから直接Notionを開いて直せる |
| `event_type:` / `sentiment:` の分布 | `other`／`neutral`偏重なら、モデルが必須項目を埋めているだけで分類できていない |
| `tickers outside universe.csv` | 形式は正しいがユニバース外。取り込まれても価格系列と結合されない（`GOOG`と`GOOGL`など） |

teiten側がQwen出力をNotionへ書く前に正規化するようになっているため、
ここが0件で安定しているのが正常。増えた場合はteiten側の正規化を疑う。

#### 規約外ページの隔離

定期実行は規約外ページを`corpus/news_rejected/part.parquet`へ退避し、
残りを取り込む。**毎回書き直す**ので、このオブジェクトは「今おかしいページ」
だけを示す（直したページは自動的に消える）。日付partitionと同じ
`corpus/*`配下なのでIAMの追加は不要。

```sql
-- 何が落ちているか
SELECT page_id, notion_url, reason FROM 's3://<bucket>/corpus/news_rejected/part.parquet';
```

ただし**過半数が規約外なら中断する**。それはページ単位の事故ではなく
schema変更や誤デプロイであり、通ったページだけでpartitionを書き直すと
残りが消えるため。ログは
`news corpus sync complete: N page(s), M rejected, ...`。

### Notionイベントスタディ

```bash
systemctl list-timers usstocks-event-study.timer
sudo systemctl start usstocks-event-study.service
journalctl -u usstocks-event-study.service -n 100 --no-pager
```

毎日13:30 JSTに、ローカルの`daily/`、`news/`、`universe/sectors.parquet`だけを
DuckDBで読む。Tiingo／Notion APIは呼ばない。出力は次のとおり。

```text
/var/lib/usstocks/corpus/analysis/
  index.json               # 日付別レポートの軽量index
  latest/                  # 下記の日次成果物の最新コピー
  daily/date=YYYY-MM-DD/
    event_returns.parquet    # event × symbol、0/1/2/5/20取引日return
    event_summary.parquet    # 軸・sample・metric・horizon別の統計
    event_unmatched.parquet  # 日足へ接続できなかったticker
    report.json              # API／過去版比較用の軽量集計
    report.md
    report.html
    manifest.json            # S3で世代ごとに最後に更新するcommit marker
```

同じ内容ならS3 uploadは0件になる。成功時はmatched/tickerイベント数とupload件数だけを
記録する。日足がまだ段階導入中のtickerは`event_unmatched.parquet`へ残り、
後続の日足timerでpartitionが増えれば次回分析で自動的に接続される。
JSTの日付ごとに成果物を残し、次回は直前の`report.json`を読み込んで件数と
全体リターンの差分を生成する。サイトのヘッダーにある「分析レポート」は、
認証済みの`/api/analysis/reports`からJSONだけを読む。APIプロセスはDuckDB／pyarrowや
イベント明細Parquetを読み込まない。

母集団が小さい間は、画面の「個別ケース分析」を先に読む。長期日足内の希少性、
イベント前モメンタム、60日出来高比、同業平均との差、同規模変動後の履歴を表示する。
同規模変動後は平均期待リターン最大の保有日と、期間重複・ばらつきを差し引いた
80%片側下限最大の保守候補を分けて表示する。
同業が`analysis_min_peers`未満の場合は参考値であり、正式なabnormal returnではない。
「銘柄フォーカス」は接続済みNotion記事イベント数が最多の銘柄を既定選択し、
記事数、実効件数、反応取引日数、イベント種別、0／1／2／5／20日の加重平均・中央値・
上昇率・peer差平均を同一銘柄内で表示する。URLの`symbol` queryで別銘柄も選択できる。
ニュース正本の`ticker`は変更せず、Micron／マイクロン、Western Digital、Seagateの
明示的な企業名を分析時だけ補完する。各ケースへ`explicit`または`inferred_alias`と
一致語を保存するため、記事数の選定根拠を監査できる。
「今回の読み取り」はLLM呼び出しではなく、同じ数値を標本数付きで文章化したルールベース出力。
根拠は各ケース下の全期間明細で検算でき、0／1／2／5／20日の未観測値も`—`として残す。

ローカルだけで確認する場合:

```bash
USSTOCKS_CORPUS_LOCAL_DIR=/path/to/corpus \
  .venv/bin/python -m usstocks.corpus.event_study --local-only
```

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

### 約定の無い足の掃除

Tiingo の1分リサンプルは、**取引が無かった分にも直前終値を持つ足**（始=高=安=終、
出来高0）を返す。`forceFill=false` を指定しても抑止されない。

```json
{"date":"...T12:06:00.000Z","open":334.53,"high":334.53,
 "low":334.53,"close":334.53,"volume":0.0}
```

これを保存すると、閑散な時間帯が「その価格で推移した」ように描かれる。薄商いの
銘柄では時間外のほとんどがこれになる。取り込み時に除外するよう修正済みだが、
**修正前に取り込んだ行は残っている**。

```bash
# 影響範囲を確認する
sudo -u usstocks sqlite3 /var/lib/usstocks/market.db \
  "SELECT symbol, COUNT(*) FROM bars_1m WHERE volume = 0 GROUP BY symbol;"

# 削除する
sudo -u usstocks sqlite3 /var/lib/usstocks/market.db \
  "DELETE FROM bars_1m WHERE volume = 0;"
```

**ライブ収集の足は影響を受けない。**collector は約定が無ければ足を作らないので、
`volume = 0` の行はRESTから来たものだけである。

削除後、次のバックフィルで取り直されることはない（同じ足は除外されるため）。
チャートには約定の無い時間帯が**空白**として現れる。これが意図した表示である
（仕様書3.3「無い動きを描かない」）。

### 銘柄カタログ（ローカル検索）

ティッカー検索は、提供元の対応銘柄一覧をローカルに持って解決する。検索が
REST枠（50回/時）を食い潰さないための仕組みである（[spec-review A-2](spec-review.md)）。

取得元は **API エンドポイントではなく静的ファイル**なので、**リクエスト数に
カウントされない**。

```
https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip
```

別の提供元のティッカー一覧を併用しない理由は、不整合を避けるためである。
検索に出た銘柄の価格が取れない、という状態が起こりうる。**同じ提供元の
universe をそのまま持つ**限り、その齟齬は原理的に発生しない。

`usstocks-catalog.timer` が**週次**（日曜 08:30 UTC、米国市場が閉まっている
時間帯）で実行する。週次で足りるのは、この間に変わるのが新規上場と上場廃止
だけで、稼働中のおよそ1万銘柄に対して週あたり数件だからである。カタログに
無い新しい銘柄も、検索が提供元へフォールバックするので引ける — その1件だけ
REST を1回使う。

zip は一時ファイルへストリーム保存し、行ごとに読み、**取り込み後に必ず削除**
する（失敗時も `finally` で消す）。`PrivateTmp=true` なので、プロセスが強制
終了しても残骸がディスクに残らない。

```bash
# 手動実行
sudo systemctl start usstocks-catalog.service
journalctl -u usstocks-catalog -n 20

# 件数の確認
sudo -u usstocks sqlite3 /var/lib/usstocks/market.db \
  "SELECT COUNT(*) FROM symbol_catalog;"
```

取り込みが1件も生まなかった場合は**失敗として扱い、前回のカタログを残す**。
配布ファイルが壊れたり形式が変わったりしたときに、検索が全滅するのを防ぐため
である。

## 5. 定期作業

| 頻度 | 作業 |
|---|---|
| 毎日 | `/api/health` の `problems` を確認（画面下部にも要約が出る） |
| 毎週 | ディスク使用率、月間受信バイト数の推移（カタログ更新は `usstocks-catalog.timer` が自動実行） |
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
