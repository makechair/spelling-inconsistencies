# 定量分析コーパス — 引き継ぎ仕様

> 目的: ニュース・決算（Notion）と株価変動を突き合わせ、定量分析を可能にする。
> 状態: **Phase 0〜2は本番稼働中。Phase 3はローカル実装・テスト完了、本番反映前**。
> 2026-07-31 時点の確定事項をまとめたもの。
> 読み方: 新しいセッションはこの1枚を読めば作業に入れる。**ここに書かれた事実を
> 再調査しないこと** — いずれも実測で確定済みで、再検証にはAPI枠と時間がかかる。

## 0. 作業の進め方（サブスク枠の制約）

Claude Pro / ChatGPT Plus は**対話UIの枠であって、APIは含まれない**。API従量課金は別会計。

| 手段 | 効果 |
|---|---|
| **セッションを短く保つ**（フェーズごとに `/clear`） | 最大。長い会話は毎ターン全履歴が再送される |
| **`/model sonnet`** | 大。実装作業はSonnet 5で十分（Opus 5 は原因未知の切り分け専用） |
| **定期LLM作業をAPIへ退避**（Haiku 4.5 + Batch） | サブスク枠を一切使わない |
| 決定的コードに寄せる | 書いた後の実行コストは永続的に0 |

**サブエージェントの複数起動はサブスク枠を節約しない。** 同じ枠から課金され、各エージェントが
コールドスタートで文脈を読み直し、親がレポートを読む分も課金される。得なのは
「大量ファイルを読んで結論だけ返す」ファンアウト検索だけで、実装作業はその形をしていない。

## 1. 再調査してはいけない確定事項

### プロバイダの実態（すべて実測、`docs/spec-review.md` A-5 / A-6）

| 事実 | 影響 |
|---|---|
| **無料枠ではWebSocketが配信されない**（Tiingo・Alpaca両方） | RESTポーリングが唯一の生存経路 |
| **IEXの時間外は実質16:00–16:45 ETまで**。AAPLでも16:43で終了、SKHYは0本 | 17:00–20:00 ETのポーリングは無駄撃ち |
| **Tiingoの分足履歴は不安定**。取得当時のデータが再問い合わせで返らない | 分足でコーパスは作れない。**日足を使う** |
| 通常取引の被覆率はAAPL 385/390本（98.7%） | 分足はイベント窓のオンデマンド取得なら実用 |
| 価格の誤差は0.08%（MU、証券会社アプリ比） | 価格そのものは信頼できる |

**SKHYの実例**（この判断の根拠）: DBには ET Jul 29 の420本（通常390 + 時間外30）が
入っているが、翌日同じ日を問い合わせると空配列が返る。Jul 28 も DB 480本に対し API は
405本しか返さない。**当時取得したデータのほうが完全**。よって S3 日次バックアップが
その履歴の唯一の記録であり、「復旧後にRESTで埋め直す」は薄い銘柄では成立しない。

### REST枠（Tiingo無料枠: 50 calls/hour, 1,000 calls/day, 1 GB/month）

| 用途 | calls/day | 備考 |
|---|---:|---|
| 前景ポーリング（4:00–16:45 ET × 約30/h） | 約383 | `min_gap` 120秒に律速され実効約2分 |
| 保険sweep（9銘柄 × **1/h** × 12.75h） | 約115 | 背景間隔は1時間 |
| 履歴取得に残る枠 | **約500** | 16:45 ET以降を止めた後の概算 |

**未確認**: 月間ユニークシンボル上限。APIから読めず、Tiingoのドキュメントは403で取得
できなかった。**50銘柄を一度に投入しないこと** — 少数ずつ増やして 4xx を観測する。

## 2. Phase 0 — 検証（完了）

2026-07-31 14時台 JST、Lightsail `usstocks-dev01` 上で AAPL だけを対象に実行した。
APIキーはサーバー内で環境変数へ渡し、値そのものは出力していない。

```bash
sudo -u usstocks env \
  USSTOCKS_TIINGO_API_KEY="$(sudo grep '^USSTOCKS_TIINGO_API_KEY=' \
    /etc/usstocks/usstocks.env | cut -d= -f2-)" \
  /opt/usstocks/current/venv/bin/python \
  /opt/usstocks/app/scripts/probe_tiingo_daily.py
```

`scripts/probe_tiingo_daily.py` は**意図的に銘柄を列挙しない**（列挙するとユニーク
シンボル枠を探査自体が消費するため）。既存銘柄1本から外挿する。

**実測結果:**

| 項目 | 結果 |
|---|---|
| provider coverage | 1980-12-12 .. 2026-07-30 |
| 指定範囲 | 1990-01-01 以降 |
| HTTP | 200 |
| 応答サイズ | 2,344,266 bytes |
| 行数 | 9,211 |
| 実データ範囲 | 1990-01-02 .. 2026-07-30 |
| 調整済みフィールド | `adjClose` / `splitFactor` / `divCash` すべて存在 |
| 最終行 | `adjClose=333.4300`, `splitFactor=1.0`, `divCash=0.0` |
| 50銘柄外挿 | 約117.2 MB（1 GB/月の11.7%）、約460,550行 |

**判定:** 全期間が1コールで返り、調整済みフィールドも揃う。ページングは不要で、
Phase 1 の日足 Parquet 方式へ進める。月間ユニークシンボル上限だけは未確認のままなので、
Phase 1 の自動実行では新規銘柄を1回3件までに制限する。

## 3. 銘柄ユニバース（AI・半導体、50銘柄）

2026-07-31 に下表を `data/universe.csv` として確定した。SEC EDGAR の SIC 分類は
50銘柄では過剰なので使わない（手書きリスト1枚で足りる）。

| subsector | symbols |
|---|---|
| logic_compute | NVDA, AMD, INTC, AVGO, QCOM, TXN, ADI, MRVL, NXPI, MCHP |
| memory_storage | MU, WDC, STX |
| foundry | TSM, GFS, UMC, STM |
| equipment | AMAT, LRCX, KLAC, ASML, TER, ENTG, ONTO, AEIS |
| analog_power_rf | ON, MPWR, SWKS, QRVO, LSCC |
| eda_ip | SNPS, CDNS, ARM |
| ai_systems_networking | SMCI, DELL, ANET, VRT, CIEN, COHR, LITE |
| ai_platform | MSFT, GOOGL, AMZN, META, ORCL, PLTR, NOW, CRM |
| emerging_silicon | ALAB, CRDO |

**SKHY（SK Hynix ADR）は入れない。** Tiingo が ET Jul 29 以降データを返さなくなった
実例であり、ユニバースに入れると無限に空レスポンスを引き続ける（`last_bar` が進まず、
隙間が広がり続けて毎周回リクエストが発生する）。現在ウォッチリストにあるなら外すこと。

## 4. ストレージ

```
s3://<既存バックアップバケット>/corpus/
  daily/symbol=NVDA/part.parquet      # 調整済み日足（全期間）
  news/date=2026-07-30/part.parquet   # Notion由来の正規化済みイベント
  universe/sectors.parquet            # symbol → subsector
```

- **DuckDB が S3 の Parquet を直接クエリする。**ウェアハウスもサーバも不要
- 50銘柄 × 30年 ≈ 37万行 → Parquet で数十MB。S3コストは月数セント
- 取得は**市場が閉じている時間帯**（09:00–17:00 JST）に走らせ、ライブポーリングと
  枠を奪い合わせない
- **同じ `RestBudget`（`api_usage` テーブル）からトークンを取ること。**
  別バケットにすると Tiingo 側の実際の上限を二重に超える

### Phase 1 実装（2026-07-31）

| 成果物 | 実装 |
|---|---|
| ユニバース | `data/universe.csv`（50銘柄、SKHYなし） |
| 取得・Parquet化 | `src/usstocks/corpus/daily.py` |
| systemd | `usstocks-corpus.service` / `.timer` |
| S3権限 | 既存backup uploaderに `corpus/*` の `PutObject` を追加 |
| 検証 | 全154テスト通過、ruff lint通過、本番初回3銘柄成功 |

無人実行の既定値は、1回10銘柄まで、そのうち新規銘柄は3件まで。新規をCSV順に
少数ずつ増やし、既存は `last_success_utc` が古い順に巡回する。APIがHTTPエラーを
返した時点で残りを止めるため、未知のユニークシンボル上限を一度に踏み抜かない。

日々の更新は直近14日を重ねて取得する。新しい `splitFactor != 1` または
`divCash != 0` を検出した場合だけ、その銘柄の全期間を再取得する。分割・配当後に
過去の `adjClose` が古いまま残るのを避けつつ、毎日全履歴を取り直す帯域浪費を防ぐ。

ローカルの `/var/lib/usstocks/corpus/state.json` は、S3 upload失敗時の再送状態も保持する。
Parquetは同じディレクトリの一時ファイルへ書き、完成後に `os.replace` するため、
途中終了したファイルが正本にならない。partitionのS3 uploadが失敗した場合はその実行の
残り銘柄も止め、APIだけを消費し続けない。

timerは **Tue–Sat 03:30 UTC（12:30 JST）**。これは直前のMon–Fri米国セッション終了後で、
合意済みの09:00–17:00 JST内に収まる。`Persistent=true` による時間外のcatch-upは
スクリプト自身が拒否し、次の定刻まで待つ。

本番導入は2026-07-31に完了した。backup uploaderには `daily/*` に加えて
`corpus/*` だけを許可し、unitを個別配置してAPIを余分に再起動せずtimerを有効化した。
初回実行はNVDA 6,922行、AMD 9,211行、INTC 9,211行（いずれも2026-07-30まで）を
取得・Parquet化・S3 uploadし、`daily corpus run complete: 3/3` で正常終了した。
本番envの `AWS_DEFAULT_REGION` に `p-northeast-1` という誤記が見つかったため、
`ap-northeast-1` へ修正済み。S3送信はAWS CLIではなくrevision venv内のboto3を使う。

```bash
systemctl list-timers usstocks-corpus.timer

# 09:00–17:00 JST内で手動実行する場合
sudo systemctl start usstocks-corpus.service
journalctl -u usstocks-corpus.service --since today
```

## 5. teiten-pipeline との統合

**別リポジトリ**（`~/Library/CloudStorage/Dropbox/stock/collection/teiten-pipeline`、
Mac ローカル、コードは GitHub に無い）。GitHub上の別セッション
「Semiconductor market monitoring pipeline」に実装内容を確認済み（2026-07-31）。
**以下は実測（コード引用）に基づく確定事項。再調査不要。**

### 実態（旧仮説からの修正）

`src/shared/summarize.py` が `requests.post` で Messages API を**同期**呼び出し
（`claude-haiku-4-5`、`template.yaml:33` で明示設定）。**Batch API ではない。**
`summarize_and_cluster(items)` が候補記事群を**1回の呼び出しで**クラスタリング＋要約
まで行っており（`app.py:67`）、「要約」と「銘柄抽出」を別々に課金している問題は
**そもそも存在しない**。よって旧セクションの ❌/✅ 対比（2重課金の解消）は的外れ
だった。本当の機会は「既存の1回の呼び出しのスキーマに新フィールドを足すだけで、
呼び出し回数を増やさずに済む」という点のみ。

**実際の出力スキーマ**（`summarize.py:56-74`）は単一オブジェクトではなく、
クラスタ単位の配列:

```json
{
  "results": [
    {
      "ids": [0, 2],
      "headline": "...",
      "summary_ja": "...",
      "my_take": "...",
      "category": "HBM|DRAM|NAND|先端パッケージ|装置|決算|統計|その他",
      "importance": 1
    }
  ]
}
```

新フィールド（`tickers` / `event_type` / `sentiment` / `confidence`）は
`results[]` の各要素（＝クラスタ＝ Notion 1ページ）に追加する。旧案の
`summary` フィールドは存在せず、`summary_ja`（事実寄り）が該当。`my_take`
（見立て・解釈）とは分離されているので、**定量分析には `summary_ja` のみ使う**。

### 決定事項（2026-07-31）

- **永続化: Notion API 読み出し方式に決定。** S3/Parquet 直書きは不採用。
  新フィールド（`tickers` / `event_type` / `sentiment` / `confidence`）は
  teiten 側で `results[]` の各要素に追加した上で、既存フィールドと同様に
  Notion プロパティとして書き込む。corpus 側（このリポジトリ）が Notion API
  経由で**既存フィールドと新フィールドをまとめて**読み出す実装を持つ
  （Phase 2 の成果物。5節冒頭の表の「対応」列を参照）。
- **処理量: 条件付きで拡大を検討する。** `MAX_LLM_ITEMS=4`（Haiku呼び出し1回
  あたりのtoken量）は据え置き — Haiku課金がtoken量に比例するため、ここを
  増やすと即コスト増になる。**その代わりクロール（収集）対象の記事母数を
  増やす方向で対応する。** クロール自体はLLMを使わないためHaiku課金には
  影響しない。ボトルネックは「収集した記事のうち、どれが上位4件に選ばれて
  要約対象になるか」を決めるスコアリング/フィルタ側。
- **カバレッジ: `ai_platform` / `eda_ip` まで広げたいが、上記の処理量対応が
  前提条件。** 現状 `category` 列挙値がメモリ・装置寄りに偏っているため、
  そのまま収集ソースだけ広げても新セクターの記事が上位4件に入らず、
  実質的にカバレッジが広がらない可能性がある。

### teiten 側への依頼事項（実装はそちらの会話で行う）

1. 収集ソース（RSS等）に `ai_platform`（MSFT, GOOGL, AMZN, META, ORCL, PLTR,
   NOW, CRM）・`eda_ip`（SNPS, CDNS, ARM）関連のニュース元を追加し、クロール
   母数を拡大する（Haiku呼び出し前の段階なのでコスト影響なし）
2. 重要度スコアリングのロジックがセクター横断で公平に上位4件を選べているか
   確認し、必要なら調整する。`category` 列挙値に `ai_platform` / `eda_ip`
   相当の値を追加するかどうかも合わせて検討（現状だと該当ニュースが
   「その他」に落ちて過小評価される可能性がある）
3. `MAX_LLM_ITEMS=4` はそのまま据え置き（Haikuのtoken消費量を変えない）
4. 新フィールド（下記スキーマ）を `results[]` の各要素に追加し、対応する
   Notion プロパティ（`Tickers` / `EventType` / `Sentiment` / `Confidence`
   等、命名はteiten側の既存命名規則に合わせる）を新設して書き込む

```json
{
  "tickers":    {"type": "array", "items": {"type": "string"}},
  "event_type": {"type": "string", "enum": [
                   "earnings","guidance","product","mna",
                   "regulatory","supply_chain","macro","other"]},
  "sentiment":  {"type": "string", "enum": ["positive","neutral","negative"]},
  "confidence": {"type": "number"}
}
```

`category`（半導体サブセクタの分類）と `event_type`（ニュースの性質）は別軸なので
併存させる。

### Phase 2 実装（2026-07-31）

teiten側の依頼事項1〜4は実装・デプロイ済みであり、SSM Parameter Storeに
`/teiten/notion-token` と `/teiten/notion-db-id` が同一AWSアカウント・東京リージョンで
存在することを値の復号なしに確認した。本リポジトリには次を追加した。

| 成果物 | 実装 |
|---|---|
| Notion読出し・正規化 | `src/usstocks/corpus/news.py` |
| 日付partition | `corpus/news/date=YYYY-MM-DD/part.parquet` |
| 定期実行 | `usstocks-news-corpus.service` / `.timer`（毎日13:00 JST） |
| credential | 直接env、またはSSMの上記2項目だけを`GetParameter` |
| 差分転送 | 全ページを論理同期し、ParquetのSHA-256が変わった日だけS3 PUT |

Notionは現状数百ページなので、全件取得でも`page_size=100`の数リクエストで済む。
増分cursorだけを正本にすると、後からの編集やアーカイブを見落とすため採用しない。
ページが全件アーカイブされた日も、同じschemaの空Parquetで既存partitionを置換する。
これによりS3の`DeleteObject`権限を追加せず、DuckDBから古いイベントが見え続けることを
防ぐ。

`XPost`はteiten側の固定形式
`summary_ja + "\n\n■見立て\n" + my_take`から2列へ分離する。新しい4項目だけでなく、
headline、category、importance、source、出典URL群、公開／作成／編集日時も同じ行へ
正規化する。未知のEventType/Sentiment、範囲外Confidence、不正tickerは同期を失敗させ、
schema driftを黙って分析データへ混ぜない。

本番初回同期は終了コード0。Notion 368ページを50日付partitionへ正規化し、
変更対象50 objectをS3へアップロードした。timerはenabled/active。

### 残課題

- **日またぎの重複統合が無い。** `ids` によるクラスタリングは同一実行内限定
  （Notion側の `Sources` 照合による重複排除はあるが、統合ではなくスキップ）。
  母数を増やした結果、日をまたいで同一イベントの記事が複数ページに分散する
  ケースが増える可能性がある。Phase 3ではページを破壊的に統合せず、
  同一銘柄・反応取引日・イベント種別ごとの重複数と重みを持たせて集計する。
- 実際のカバレッジ（`ai_platform`/`eda_ip` の記事が上位4件に入るようになったか）は
  運用データで継続確認する。これはPhase 2の導入ブロッカーではない。

## 6. フェーズ

| # | 状態 | 内容 | 成果物 | 依存 |
|---|---|---|---|---|
| 0 | **完了** | 日足エンドポイントの検証 | プローブ出力 | — |
| 1 | **完了（本番稼働中）** | 日足コーパス | `universe.csv`, 取得スクリプト, systemd timer, S3 Parquet | 0 |
| 2 | **完了（本番稼働中）** | Notion取り込み + 抽出統合 | `corpus/news.py`, systemd timer, S3 Parquet | 0 |
| 3 | **実装済み（本番反映前）** | イベントスタディ | DuckDB SQL / Parquet / Markdown・HTML report | 1, 2 |
| 4（任意） | 未着手 | イベント窓の分足オンデマンド取得 | 既存 backfill の再利用 | 3 |

Phase 1 と Phase 2 は独立しており、どちらも本番稼働まで完了した。teiten 実装の
実態確認も完了している（5節）。次の実装対象はPhase 3。

### Phase 3 の設計到達点

Phase 3は**日足によるイベントスタディを先に作る**。日足とニュースの入力schema、
S3配置、計算仕様、SQL、テスト、レポート生成までローカル実装済み。systemd unitの
本番配置と初回実データ実行は未実施。

#### 反応日の決め方

1. `published_at` に時刻とoffsetがあれば `America/New_York` に変換する。
2. 米国取引日の16:00 ETより前ならその日、16:00 ET以降なら次の取引日を
   `reaction_date` とする。週末・休場日も次の取引日へ送る。
3. `published_at` が日付だけ、または欠落して `event_date` にフォールバックした行は
   `timing_quality=date_only` とする。結果には残すが、時刻精度が必要な検定とは分ける。
4. 取引日判定はカレンダー日加算ではなく、各銘柄の日足に存在する日付列を使う。

日中発表は発表前の値動きを日足から分離できない。この初版は因果推定ではなく
「そのニュースと同日以降の値動きの関連」を測るものと明記する。

#### リターン

銘柄ごとに`reaction_date`を`t=0`とし、直前取引日の調整済み終値を基準にする。

```text
raw_return_h = adjClose[t+h] / adjClose[t-1] - 1
h = 0, 1, 2, 5, 20取引日
```

- 分割・配当をまたぐため、必ず`adjClose`を使う
- 欠損した取引日を0で補間しない。必要な端点が無いhorizonは`NULL`とする
- 初版の比較対象は、同じ`subsector`に属する銘柄の等ウェイトリターン
  （対象銘柄を除外、最低3銘柄）とする
- `abnormal_return_h = raw_return_h - subsector_return_h`
- SPY/QQQ/SMH等の外部benchmarkは、Tiingoの未知の月間ユニークシンボル枠を
  新たに消費するため初版の必須条件にしない

#### 重複・重なり

- Notionページは正本なので削除・自動マージしない
- `symbol + reaction_date + event_type`単位の`event_group_size`を出し、
  集計時の既定ウェイトを`1 / event_group_size`とする
- 同じ銘柄で20取引日窓が重なる別イベントには`overlap_count`を付ける
- 記述統計には全件を残し、信頼区間・有意性を見る集計では重複窓を除外した結果も併記する

#### 出力と切り口

`event_returns.parquet`は最低限、`page_id`, `symbol`, `reaction_date`,
`timing_quality`, `event_type`, `sentiment`, `confidence`, `importance`,
`subsector`, 各horizonの`raw_return`/`abnormal_return`,
`event_group_size`, `event_weight`, `overlap_count`を持つ。

集計は件数だけでなく中央値、平均、勝率、四分位、95%信頼区間を出し、次の軸で切る。

- `event_type`
- `sentiment`
- `subsector`
- `confidence`帯
- `importance`
- 発表タイミング（pre-market / regular / after-hours / date-only）

初版の成果物はDuckDB SQL、fixtureを使った境界テスト、Parquet出力、
Markdown/HTMLレポートとする。現在の1分足チャートへのニュースmarker表示は別機能であり、
Phase 3の集計結果が妥当と確認できてからAPI/UIを追加する。Phase 4は必要なイベントだけ
分足をオンデマンド取得し、日中の反応窓を細分化する任意拡張とする。

#### Phase 3 実装（2026-07-31）

| 成果物 | 実装 |
|---|---|
| 計算本体 | `src/usstocks/corpus/event_study.py` |
| DuckDB SQL | `src/usstocks/corpus/sql/event_study.sql` |
| event明細 | `analysis/latest/event_returns.parquet` |
| 集計 | `analysis/latest/event_summary.parquet` |
| 未接続ticker | `analysis/latest/event_unmatched.parquet` |
| 人向け出力 | `analysis/latest/report.md`, `report.html` |
| commit marker | `analysis/latest/manifest.json`（S3 uploadは常に最後） |
| 定期実行 | `usstocks-event-study.service` / `.timer`（毎日13:30 JST） |

DuckDBは1 thread、memory limit 256MB。systemd側は`MemoryMax=512M`で囲う。Phase 1/2の
ローカルParquetだけを読み、Tiingo／Notion APIを呼ばない。同一内容の再実行はSHA-256で
判定してS3 PUTを0件にする。manifestを最後に送るため、利用側はmanifestが指すdigestを
完全な世代として扱える。

fixtureでは、16:00 ET境界、週末、時刻なし、同一イベントの1/N重み、20取引日窓の
重なり、未収録ticker、最低3 peerのsubsector benchmark、同一入力の再実行でupload 0を
検証した。リポジトリ全体は157テストとruffを通過。

## 7. 未処理のTODO（本体側）

- [x] `background_poll_seconds` のコード既定値と `.env.example` を 1800 → 3600 に変更
      （本番envは未指定のため、デプロイ後は3600秒のコード既定値が有効）
- [x] 本番 `AWS_DEFAULT_REGION` の誤記を `ap-northeast-1` へ修正
- [x] SKHYのwatch/holdを解除（空レスポンスによるREST枠消費を停止）
- [x] 清掃直前のS3バックアップ成功後、`volume=0`を8,134行削除
      （AAPL 2,966 / MU 2,263 / SKHY 2,905、削除後0行）
- [ ] Alpaca APIキーのローテーション（チャットに露出済み）
- [x] RESTポーリングを通常16:45 ET、短縮取引日は通常終了45分後に停止
      （市場カレンダーの時間外定義自体は変更しない）
- [x] 成功したfetchが3回連続0本ならsymbolを警告状態にし、次の有効データで自動解除

## 8. 一次情報

| 対象 | ファイル |
|---|---|
| 全体構成・なぜ動くのか | `docs/system-architecture.html` §2, `docs/system-architecture.md` §2 |
| プロバイダ実測の根拠 | `docs/spec-review.md` A-5 / A-6 |
| REST枠の制御 | `src/usstocks/collector/service.py` `_poll_loop()`, `backfill.py`, `ratelimit.py` |
| 日足プローブ | `scripts/probe_tiingo_daily.py` |
| 分足プローブ | `scripts/probe_tiingo_rest.py` |
| WebSocketプローブ | `scripts/probe_tiingo_ws.py`, `scripts/probe_alpaca_ws.py` |
| 運用手順 | `docs/operations.md` |
