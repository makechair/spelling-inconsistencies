# 決算分析 — 仕様（ドラフト、着手前レビュー用）

> 状態: **未実装。この文書はレビュー用の提案。** 合意後に実装する。
> 位置づけ: ニュース×株価のイベントスタディ（`analysis-spec.md`）とは**別立て**。
> あちらは「特定の出来事に株価がどう反応したか」、こちらは「企業の実力と
> 見通しが決算数値からどう見えるか」で、時間軸も判断材料も別物。

## 1. データ源 — SEC EDGAR XBRL

**`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`** を使う。

| 特性 | 内容 |
|---|---|
| 費用 | 無料。APIキー不要 |
| **Tiingo枠** | **一切消費しない。** 別系統なので既存のREST予算と競合しない |
| 必須 | `User-Agent` に連絡先。無いと403 |
| レート | 10 req/秒。53銘柄なら1回の全取得でも数秒 |
| 形式 | XBRL。提出済みの全期間の数値が概念別に入る |

### 確認済みの制約（実測ではなく既知の仕様）

**1. この開発コンテナからEDGARへ到達できない。**
`www.sec.gov` はこの環境のネットワークポリシーで403（CONNECT拒否）。
Lightsailからは通るはずだが、**レスポンス形状をローカルで実測できない**。
したがって実装はfixtureベースで書き、**初回の本番実行が実質的な検証**になる。
最初の1銘柄だけを対象にできるCLIオプションを必ず用意する。

**2. 外国籍企業は四半期決算が無い。**
20-F提出者（`TSM` `ASML` `UMC` `STM` `ARM` `ASX` 等）は**年1回**の提出で
10-Qが無い。四半期の横並び表ではこれらが欠測になる。
**欠測を0や前期据え置きで埋めない** — 「四半期データなし」と明示する。
年次指標は全銘柄で比較可能なので、**一元表示は年次を主、四半期を従**とする。

**3. XBRLタグは企業ごとに揺れる。** 同じ「売上高」でも
`Revenues` / `RevenueFromContractWithCustomerExcludingAssessedTax` /
`RevenueFromContractWithCustomerIncludingAssessedTax` が混在する。
概念ごとに**優先順位付きのタグ候補列**を持ち、最初に見つかったものを採る。
**どのタグを採ったかを行に残す**（後から異常値を追跡するため）。
候補が全滅した概念は欠測にし、推定しない。

## 2. 指標

すべて**XBRLの数値から決定的に算出**する。LLMは一切計算しない。

### 成長

| 指標 | 定義 |
|---|---|
| 売上YoY | 直近期 / 前年同期 - 1 |
| 売上YoYの変化 | 今期YoY - 前期YoY（**増収の加速・減速**。水準より転換点が重要） |
| 3年CAGR | 年次売上の年平均成長率 |

### 収益性

粗利率・営業利益率・純利益率と、**それぞれの前年同期差**。
半導体は稼働率で粗利率が大きく振れるため、水準と方向を併記する。

### 半導体サイクル（この分析の主眼）

| 指標 | 定義 | なぜ見るか |
|---|---|---|
| **在庫日数（DIO）** | 棚卸資産 / 売上原価 × 期間日数 | **サイクルの先行指標。** 在庫の積み上がりは需要減速に先行する |
| DIOの前年同期差 | | 水準は企業ごとに違うので、変化を見る |
| 設備投資対売上比 | capex / 売上 | 供給能力の増減。業界全体で見ると次サイクルの供給過剰を示唆 |
| R&D対売上比 | R&D / 売上 | 将来の競争力への投下 |

### 財務健全性

自己資本比率、純有利子負債 / EBITDA、営業CF、FCF（営業CF − capex）、FCFマージン。

### 株価との接続（日足コーパスを再利用）

PER、PBR、EV/EBITDA、FCF利回り。**日足コーパスが既にあるので追加取得は不要。**
株数はXBRLの発行済株式数を使う。

## 3. 「将来性」の扱い

**方針: 実績トレンドから機械的に算出し、Qwenは解釈文だけを書く。**

`narrative.py` と同じ型を使う。モデルには**数値を計算させず、復唱もさせない**。
提示するfact IDの選択と、その意義の説明だけをさせ、読者に見せる数値は
こちらがJSONからコピーする。ローカルLLMの幻覚が数字へ混入しない。

将来性の材料は次の決定的な量に限る。

- 増収率の加速/減速（上記「売上YoYの変化」）
- マージンの方向
- DIOの方向（在庫調整局面か、需要回復局面か）
- capex対売上比の方向（供給拡大か抑制か）
- R&D対売上比の水準と方向

**ガイダンス本文（MD&A等）の読み込みは初版に含めない。** 情報量は増えるが、
本文取得とLLM依存が増え、記述粒度が銘柄ごとに違うため一元表示の比較可能性が
下がる。実績トレンド版を動かしてから、必要なら追加する。

## 4. ストレージ

```
s3://<既存バックアップバケット>/corpus/
  fundamentals/symbol=NVDA/part.parquet   # 概念別の時系列（提出期別）
  fundamentals_metrics/part.parquet       # 全銘柄・全期の算出済み指標
  fundamentals_rejected/part.parquet      # タグ全滅・異常値の隔離（news_rejectedと同じ考え方）
```

既存の `corpus/*` IAM権限に収まるのでIAM変更は不要。
DuckDBがS3のParquetを直接クエリする点も既存と同じ。

## 5. 画面

**既存の `reports.html`（ニュース×株価）とは別ページ**にする。

### 5-1. 一元表示 `web/fundamentals.html`

53銘柄 × 主要指標の表。**年次を主**（20-F提出者も比較できる）。

- subsectorでグループ化・絞り込み
- 列ソート
- 各指標セルは「水準」と「前年差」を併記。改善/悪化を色で示す
- **四半期データが無い銘柄は空欄と明示**（0で埋めない）
- 銘柄名クリックで詳細へ

### 5-2. 詳細 `web/fundamentals.html?symbol=NVDA`

- 売上・各利益率・DIO・capex比・FCFの時系列グラフ（年次＋四半期）
- 同subsector中央値との比較（peerは自分を除く。`analysis-spec.md` の
  `min_peers` と同じ考え方）
- 直近決算の要点（Qwenの解釈文＋根拠数値）
- 採用したXBRLタグと提出書類へのリンク（監査可能性）

## 6. 定期実行

`usstocks-fundamentals.timer`。決算は不定期に出るので**毎日1回**、
提出の有無を確認して差分だけ取る（`accn` 単位で既知を飛ばす）。
市場が閉じている時間帯（09:00–17:00 JST）に走らせる方針は既存と同じ。
Tiingo枠を使わないので日足コーパスとの競合は無いが、
CPU・帯域の山を作らないため時刻はずらす。

## 7. 段階

| # | 内容 | 成果物 | 状態 |
|---|---|---|---|
| A | EDGAR取得と正規化 | `corpus/fundamentals.py`、1銘柄限定CLI、fixtureテスト | **実装済み・本番未検証** |
| B | 指標算出 | `fundamentals_metrics` Parquet、DuckDB SQL | 未着手 |
| C | 一元表示 | `web/fundamentals.html`、API | 未着手 |
| D | 詳細＋Qwen解釈 | 詳細ページ、`narrative.py` の型を再利用 | 未着手 |

**Aの初回本番実行までEDGARのレスポンスを実測できない**ため、Aを本番で
通してからBへ進む。Aで1銘柄だけ取れることを確認するのが最初の関門。

### Phase A 実装（2026-08-04）

| 成果物 | 実装 |
|---|---|
| 取得・正規化 | `src/usstocks/corpus/fundamentals.py` |
| partition | `corpus/fundamentals/symbol=NVDA/part.parquet` |
| 定期実行 | `usstocks-fundamentals.service` / `.timer`（毎日14:30 JST） |
| 設定 | `USSTOCKS_SEC_USER_AGENT`（必須。無いとSECは403） |
| 検証 | 187テスト・ruff通過。**EDGAR実レスポンスは未検証** |

CIKは `company_tickers.json` から実行時に引く（静的コピーはticker移管で
黙って腐る）。行は概念×提出期で、**採用したXBRLタグを列に持つ**。
`accn` 単位で行を分けるので、訂正報告（10-Q/A）は元の行を上書きせず並ぶ。
残高項目（在庫・資産等）と期間項目（売上等）は `start` の有無で判別し、
混同しない。同一Parquetのdigestが変わらなければS3へ再送しない
（EDGARは毎回全期間を返すため、これが無いと毎日全量転送になる）。

SECがエラーを返した時点で残りを止める。連打はSECにブロックされる典型
経路であり、1回失敗したものは次も失敗する可能性が高い。

**最初にやること**（1銘柄で形状を確認する。本番のみ実行可能）:

```bash
sudo bash -c 'cd /var/lib/usstocks && set -a; . /etc/usstocks/usstocks.env; set +a; \
  USSTOCKS_CORPUS_LOCAL_DIR=/var/lib/usstocks/corpus \
  USSTOCKS_CORPUS_UNIVERSE_PATH=/opt/usstocks/current/data/universe.csv \
  runuser -u usstocks -- /opt/usstocks/current/venv/bin/python \
    -m usstocks.corpus.fundamentals --force --symbols NVDA'
```

`/etc/usstocks/usstocks.env` に `USSTOCKS_SEC_USER_AGENT` を先に足すこと。
成功したら `--symbols MU,TSM` で、10-K提出者と20-F提出者の差を確認する。

## 8. 未決事項

- 一元表示に出す指標の最終選択（上記は提案。多すぎると読めない）
- 「将来性」をスコア化して1列にするか、指標を並べるだけにするか。
  スコアは一覧性が上がるが、重み付けの根拠が無いと恣意的になる
- 過去何年分を取るか（EDGARは全期間返す。表示は5年程度が実用的か）
