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
Mac ローカル）。既に Haiku で記事要約を生成しており、API課金の口が既にある。

**本命の節約は「バッチ共有」ではなく「同じ記事に2回課金しないこと」。**

```
❌ 記事本文 ─→ [Haiku: 要約]     ─→ Notion
   記事本文 ─→ [Haiku: 銘柄抽出] ─→ corpus      ← 入力トークンを2回払う

✅ 記事本文 ─→ [Haiku: 要約 + 銘柄抽出] ─┬─→ Notion
                                        └─→ corpus
```

入力（記事本文）が支配的なので、1回にまとめると**合算ワークロードが約半分**。
追加は出力トークン数百のみ。Batch の50%引きと合わせて素直な同期実装比**約25%**。

既存スキーマに足すフィールド:

```python
output_config={"format": {"type": "json_schema", "schema": {
  "type": "object",
  "properties": {
    "summary":    {"type": "string"},                    # teiten 既存
    "tickers":    {"type": "array", "items": {"type": "string"}},
    "event_type": {"type": "string", "enum": [
                     "earnings","guidance","product","mna",
                     "regulatory","supply_chain","macro","other"]},
    "sentiment":  {"type": "string", "enum": ["positive","neutral","negative"]},
    "confidence": {"type": "number"},
  },
  "required": ["summary","tickers","event_type","sentiment","confidence"],
  "additionalProperties": False,
}}}
```

**注意点:**

- **Haiku 4.5 の prompt cache 最小長は 4,096 トークン**（Opus 5 は 512）。共通の抽出
  指示がこれを下回ると、**エラーも警告もなく黙ってキャッシュされない**。この規模なら
  無理に超えさせず、Batch の50%引きだけで十分
- teiten が同期実行なら **Batch へ移すだけで既存分も50%引き**。日次収集なら24時間以内の
  完了は問題にならない
- 構造化出力なのでパース失敗のリトライが構造的に消える

**着手前に必要な情報**（未取得。Macローカルのため参照不可）:
1. 現在の抽出呼び出し — モデル、同期かBatchか、プロンプトの形
2. 出力スキーマ / Notion プロパティの対応
3. 1日あたりの記事本数

## 6. フェーズ

| # | 内容 | 成果物 | 依存 |
|---|---|---|---|
| 0 | 日足エンドポイントの検証 | プローブ出力 | — |
| 1 | 日足コーパス | `universe.csv`, 取得スクリプト, systemd timer, S3 Parquet | 0 |
| 2 | Notion取り込み + 抽出統合 | teiten スキーマ拡張, corpus への書き出し | teiten情報 |
| 3 | イベントスタディ | DuckDB クエリ / ノートブック | 1, 2 |
| 4（任意） | イベント窓の分足オンデマンド取得 | 既存 backfill の再利用 | 3 |

Phase 1 と Phase 2 は独立しており、並行して進められる。

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
