# 定量分析コーパス — 引き継ぎ仕様

> 目的: ニュース・決算（Notion）と株価変動を突き合わせ、定量分析を可能にする。
> 状態: **Phase 0 未実行**。この文書は 2026-07-30 時点の確定事項をまとめたもの。
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
| 前景ポーリング（16h × 約30/h） | 480 | `min_gap` 120秒に律速され実効約2分 |
| 保険sweep（9銘柄 × **1/h** × 16h） | 144 | **30分→1時間へ変更する**（下記TODO） |
| 履歴取得に残る枠 | **約370** | この配分で合意済み |

**未確認**: 月間ユニークシンボル上限。APIから読めず、Tiingoのドキュメントは403で取得
できなかった。**50銘柄を一度に投入しないこと** — 少数ずつ増やして 4xx を観測する。

## 2. Phase 0 — 検証（次にやること）

```bash
sudo -u usstocks env \
  USSTOCKS_TIINGO_API_KEY="$(sudo grep '^USSTOCKS_TIINGO_API_KEY=' \
    /etc/usstocks/usstocks.env | cut -d= -f2-)" \
  /opt/usstocks/current/venv/bin/python \
  /opt/usstocks/app/scripts/probe_tiingo_daily.py
```

`scripts/probe_tiingo_daily.py` は**意図的に銘柄を列挙しない**（列挙するとユニーク
シンボル枠を探査自体が消費するため）。既存銘柄1本から外挿する。

**判定基準:**

| 結果 | 次の行動 |
|---|---|
| `adjClose` / `splitFactor` / `divCash` が揃う | Phase 1 へ進む |
| **調整済みフィールドが無い** | **日足案を組み直す。**分割をまたぐイベントスタディが50%の暴落に見える |
| 全期間が1コールで返らない | ページングの実装が要る。calls/銘柄の見積りを修正 |

## 3. 銘柄ユニバース（AI・半導体、50銘柄）

**下書き。着手前に編集すること。** SEC EDGAR の SIC 分類は50銘柄では過剰なので使わない
（手書きリスト1枚で足りる）。`data/universe.csv` に `symbol,subsector` で置く想定。

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

### 新たに判明したブロッカー

- **構造化出力は永続化されていない。** API レスポンスは Notion へ書き込んだ後
  破棄される（`summarize.py` 内でパース後に捨てる）。corpus 側にフィールドを
  増やしても、**保存先を新設しない限り読み出せない**。選択肢は次の2つ:
  1. teiten 側で LLM 呼び出し直後に corpus 用ストレージ（S3/Parquet）へ直接書く
  2. 新フィールドを Notion プロパティとしても書き込み、後から Notion API 経由で
     読み戻す（Notion のレート制限と型変換が追加で乗る）
- **処理量が想定よりはるかに少ない。** 収集自体は毎回300件超あるが、
  `MAX_LLM_ITEMS=4`（`template.yaml:35`）× 1日3回実行（6/12/18時JST）で、
  LLM が要約するのは**最大12件/日**。クラスタ統合でNotionページ数はさらに
  少ない。イベントスタディの母数として十分か要検討（本セクション末の判断参照）。
- **ニュースの対象範囲が半導体メモリ・装置寄り。** `category` の列挙値
  （HBM/DRAM/NAND/先端パッケージ/装置/決算/統計/その他）は teiten の収集源が
  メモリ・半導体装置に特化していることを示唆する。3節のユニバース50銘柄には
  `ai_platform`（MSFT, GOOGL, AMZN, META, ORCL, PLTR, NOW, CRM）や
  `eda_ip`（SNPS, CDNS, ARM）等、teiten の収集範囲に入っているか不明な銘柄が
  多く含まれる。カバレッジは Phase 2 着手前に確認が要る。
- **クラスタリングは実行内限定。** `ids` による統合は同一実行の候補間のみで、
  日をまたいだ重複記事の統合はしない（Notion側の `Sources` 照合による重複排除は
  あるが、統合ではなくスキップ）。

### 新フィールド案（`results[]` の各要素に追加）

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
併存させる。実装自体は teiten 側の会話で進める（このリポジトリの管轄外）。

## 6. フェーズ

| # | 内容 | 成果物 | 依存 |
|---|---|---|---|
| 0 | 日足エンドポイントの検証 | プローブ出力 | — |
| 1 | 日足コーパス | `universe.csv`, 取得スクリプト, systemd timer, S3 Parquet | 0 |
| 2 | Notion取り込み + 抽出統合 | teiten スキーマ拡張, corpus への書き出し | 永続化方式の決定、カバレッジ確認 |
| 3 | イベントスタディ | DuckDB クエリ / ノートブック | 1, 2 |
| 4（任意） | イベント窓の分足オンデマンド取得 | 既存 backfill の再利用 | 3 |

Phase 1 と Phase 2 は独立しており、並行して進められる。teiten 実装の実態確認は
完了した（5節）。Phase 2 着手前に残る判断は5節末の「新たに判明したブロッカー」
3点（永続化方式／処理量の十分性／ニュース対象範囲のカバレッジ）。

## 7. 未処理のTODO（本体側）

- [ ] `background_poll_seconds` を 1800 → 3600 に変更（履歴用に144 calls/day を捻出）
- [ ] SKHY をウォッチリストから削除（空レスポンスを引き続けて枠を消費している）
- [ ] `DELETE FROM bars_1m WHERE volume = 0`（出来高0の足の掃除、未実行）
- [ ] Alpaca APIキーのローテーション（チャットに露出済み）
- [ ] 17:00–20:00 ET のポーリング抑止を入れるか判断（IEXにデータが存在しない時間帯。
      カレンダーは市場として正しいので、抑止するならポーリング側の設定にする）
- [ ] 「取引時間中に成功したfetchがN回連続0本」の検出と銘柄への⚠表示
      （SKHY型の静かな停止。閾値と扱いは未決）

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
