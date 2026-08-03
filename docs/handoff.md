# 引き継ぎドキュメント（Claude → 次の担当AI/人間）

作成日: 2026-07-31、最終更新: 2026-08-03。**このファイルはリポジトリに残す前提で
書いている（消さないこと）。作業が進んだら追記・更新して次の引き継ぎに使ってよい。**

**§4-6「Notion取り込みの改善」は2026-08-03に完了**（teiten側の正規化＋
corpus側の隔離）。**次の焦点は §4-7「tickerが付かない362ページ」** —
取り込みが直っても、価格と突き合わせられるのは418ページ中55ページしかない。

## 0. まず読むもの（優先順）

1. このファイル（全体像・直近の状況・ハマりどころ）
2. **`docs/analysis-spec.md`** — 現在進行中の作業（定量分析コーパス）の正式リファレンス。
   **ここに書かれた事実は再調査しないこと**（実測で確定済み。再検証にはAPI枠と時間がかかる）
3. `docs/spec-review.md` — 本体（リアルタイムチャート部分）の仕様レビュー。未定義判断への決定を含む
4. `README.md` — セットアップ・運用コマンド
5. このリポジトリに `CLAUDE.md` は無い。上記4ファイルが実質的にその役割を兼ねている

矛盾があれば `docs/analysis-spec.md` が最新（このファイルはスナップショット）。

## 1. これは何か

**本体**: 保有する米国株の1分足を常時収集し、本人だけがアクセスできるWebチャートで
表示するシステム（`README.md` 参照）。Tiingo WebSocket+REST（障害時 Alpaca IEX予備）、
FastAPI、SQLite、SSE配信。**これは既に動いている運用中のシステム**（systemd + Lightsail、
`docs/systemd-deployment.md` / `docs/aws-deployment.md`）。

**現在進行中の作業**: 上記の本体とは別に、**半導体銘柄の株価×ニュース定量分析コーパス**
を構築する計画が `docs/analysis-spec.md` に整理されている。ニュース側は
`makechair/us-stock-realtime-chart` とは別の GitHub リポジトリを持たない別プロジェクト
「teiten-pipeline」（Mac ローカル、Claude Codeの別会話で開発）が担当し、Notion DB に
記事要約を溜めている。両者を DuckDB + S3 Parquet で突き合わせる設計。

```
[本体] Tiingo/Alpaca → collector → SQLite → API/Web（常時稼働、既存）
[新規] Tiingo daily bars → S3 Parquet ─┐
       teiten の Notion DB (要約+tickers/event_type/sentiment/confidence)
                                     → DuckDB でクエリ → イベントスタディ
```

## 2. 今すぐ知っておくべき直近の変更（2026-07-31セッション）

当初のClaudeセッションは設計・意思決定のみだったが、Codex引き継ぎ後の
2026-07-31セッションで Phase 0〜2 の実装まで進んだ。実施したのは:

1. `docs/analysis-spec.md` を新規作成 — Phase 0〜4の作業計画、REST枠の配分、
   50銘柄ユニバース（`data/universe.csv`）、S3ストレージ設計
2. teiten-pipeline との統合方針を、**あちら側の会話で実装コードを確認してもらった上で**
   確定（`docs/analysis-spec.md` 5節）。要点:
   - 永続化は **Notion API 経由で読み出す**方式に決定（S3直書きは不採用）
   - teiten 側で `tickers`/`event_type`/`sentiment`/`confidence` を追加し、
     Notionプロパティ `Tickers`/`EventType`/`Sentiment`/`Confidence` として書込済み
     （teiten側で実装・デプロイ完了、2026-07-31 02:40 UTC）
   - teiten 側は収集セクターを `ai_platform`/`eda_ip` まで拡大済み、
     Haiku呼び出しコストは変えていない（セクター最低枠保証で対応）
3. Phase 0をLightsailで実行済み。AAPLはHTTP 200、2,344,266 bytes、9,211行、
   1990-01-02〜2026-07-30、`adjClose`/`splitFactor`/`divCash`あり。50銘柄外挿は
   約117.2 MB（1 GB枠の11.7%）。詳細は `docs/analysis-spec.md` 2節
4. Phase 1を実装し、全141テストとruff lintが通過:
   - `data/universe.csv`（50銘柄）
   - `src/usstocks/corpus/daily.py`（共有REST予算、Parquet、S3 upload）
   - `deploy/systemd/usstocks-corpus.service` / `.timer`
   - `infra/terraform/backup.tf` の `corpus/*` 書き込み権限
   - 新規銘柄は1回3件、全体10件まで。HTTPエラーで残りを停止
5. Phase 1を`main`へ反映し、GitHub Actionsを使わずLightsailのpull agentで本番導入済み:
   - `usstocks-corpus.timer` はenabled/active（Tue–Sat 12:30 JST）
   - IAMは既存bucketの `daily/*` と `corpus/*` へのPutObject/Listだけ
   - 初回はNVDA 6,922行、AMD 9,211行、INTC 9,211行をS3へ送信し3/3成功
   - 本番envの誤記 `AWS_DEFAULT_REGION=p-northeast-1` は
     `ap-northeast-1` へ修正済み
6. Phase 2を実装:
   - `corpus/news.py`でNotion全ページを日付別Parquetへ正規化
   - SHA-256が変わったpartitionだけS3へPUTし、アーカイブ日は空partitionで置換
   - SSMに必要な2パラメータが存在することを値の復号なしで確認
   - IAMはこの2項目の`GetParameter`だけを既存host userへ追加
   - Lightsailで初回同期を実行し、368ページ・50日付partition・50 S3 objectを確認
   - `usstocks-news-corpus.timer` はenabled/active（毎日13:00 JST、最大10分の遅延）
7. 本体側の残件を実装:
   - REST pollingを通常16:45 ETで停止（短縮取引日対応）
   - 3回連続の成功空fetchをsymbol警告として表示し、復旧時に自動解除
   - SSE接続中のAPI停止を10秒で打ち切り、deploy停止時間を短縮
   - 清掃直前のS3バックアップ成功後、`volume=0` の8,134行を削除
     （AAPL 2,966 / MU 2,263 / SKHY 2,905）
   - 誤登録SKHYのwatch/holdを解除
8. チャートUIを実装・本番反映:
   - 既定画面を1D・7D・1M・1Yの4分割へ変更
   - 各チャートまたは期間ボタンで拡大し、「4分割」でoverviewへ復帰
   - 4画面は1回の`days=365`応答を共有し、通信の4重化を回避
   - 上限超過時も古い側ではなく最新20,000本を返すようAPIを修正
   - 全154テスト、ruff、実ブラウザで4分割／拡大／期間切替／復帰を確認
   - `[skip ci]`付きでmainへ反映し、GitHub Actionsを使わずLightsail pull agentで
     revision `34c1ce8f99d3ec15ff5331309bad5f684f1b91f`へ更新
   - API/collector/日足corpus/Notion corpusの4 unitがactive、`/api/livez`正常、
     本番配置済みHTMLに1/7/30/365日の4ペインがあることを確認
9. Phase 3イベントスタディを実装・本番反映:
   - `corpus/event_study.py` + DuckDB SQLで0/1/2/5/20取引日returnを計算
   - 同subsector最低3 peerの相対return、日またぎ重複weight、窓重複を実装
   - event明細／集計／未接続tickerのParquet、Markdown／HTML、manifestを生成
   - 毎日13:30 JSTのsystemd oneshot/timerを追加し、enabled/activeを確認
   - 全157テストとruffを通過
   - 本番初回は368ページ／17 tickerイベント中1件matched、16件unmatched、
     6ファイルをS3へupload。再実行は0 upload、CPU時間約1.3秒

コミット履歴（このセッション分、新しい順）:

```
ef52c10 Implement the Phase 3 event study [skip ci]
34c1ce8 Add iSPEED-style four-pane charts [skip ci]
ee16839 Complete Notion corpus and data-quality hardening [skip ci]
47a04ee Turn the teiten open questions into decisions
9ee7c56 Replace the teiten integration guess with what the code actually does
4822043 Write down what a fresh session needs so it does not re-derive it
```

## 2-B. Phase 3 以降の追加実装（〜2026-08-03、`a277566` まで）

上の §2 は `ef52c10` で終わっているが、その後さらに31コミットが入っている。
概要は以下（詳細はコミットと `docs/aws-deployment.md`）。

**分析レポート基盤**（`web/reports.html` / `reports.js`、`api/routes/analysis.py`）

- 日次の分析レポートと履歴。イベント別・ticker別のケース分析
- horizon別リターン分布、peerと自社の反応の分離
- **リターンサーフェス** — 初日変動率（5〜95 percentileの17点）× 1〜20取引日を
  Gaussian kernelで平滑化し、無条件平均に対する超過リターンを色で示す。
  上下5%は急落・急騰の独立帯。設計根拠は `analysis-spec.md` 6節に詳しい
- **リスク調整済み売買ペア** — B（1〜19日後に買い）→ S（Bより後〜20日後に売り）の
  全組み合わせを集計し、往復コスト0.10%と期間重複を考慮した標準誤差を引いた
  80%片側下限が最大のペアを上位3件表示
- **アウトオブサンプル検証** — 最初の55%を学習、55〜70/70〜85/85〜100%を
  順に検証するexpanding-window。条件付き期待値の校正であり、
  portfolio backtestではないとWeb上に明記している

**データ蓄積状況ページ**（`web/coverage.html` / `coverage.js`、`api/routes/coverage.py`）

分足DBの連続取得期間を表示。日足corpusとは分けている。

**長期日足のチャート統合**（`api/routes/bars.py`、`web/chart.js`）

日足corpusを分足DBと結合し、週足を含む長期軸をチャートで扱えるようにした。

**ローカルQwenブリッジ**（`scripts/run_analysis_narrative_bridge.py`、`corpus/narrative.py`）

Lightsailで生成した `report.json` をS3の専用mailbox経由でMacへ渡し、
ローカルOllama（Qwen3 14B）が考察を書いて `ai_digest.json` を返す。
Lightsailの15分タイマーが日次レポートへ取り込む。**LLM費用ゼロ。**

設計上の要点: **Qwenに数値を計算・復唱させない。** モデルはfact IDの選択と
意義の説明だけを行い、読者に見せる数値は `report.json` からこちらでコピーする
（`narrative.py` 冒頭のdocstring）。ローカルLLMの幻覚が数字へ混入しない。

IAMは専用ユーザーで `input/latest/report.json` の読取と日付別 `ai_digest.json` の
書込のみ。同じレポートはSHA-256で判定して再処理しない。
セットアップ手順は `docs/aws-deployment.md` の「ローカルQwen分析要約の鍵」。

## 3. リポジトリ／ブランチの実態

2026-07-31にローカル設定を再確認した結果:

- remoteは **`origin = git@github.com:makechair/us-stock-realtime-chart.git`** の1つだけ。
  旧 `spelling-inconsistencies` remoteや`newrepo` remoteは現在の `.git/config` に無い。
- 現在地は `main`。Phase 1と本番検証中に判明した修正はコミット・push済み。
- Lightsailも同じ`main`のsystemd revisionで稼働し、API healthを確認済み。
- `main`をpushすると、GitHub Actionsの結果を待たずLightsailのpull agentが検知して
  collector/APIを再起動する。Actions枠を使い切っている月は特に、ローカルテスト完了を
  確認してからpushする。
- `wip/local-systemd`ブランチは残っているが、現行systemd運用は既に`main`へ統合済み。
  Phase 1の作業で切り替える必要はない。

**環境によってremote構成が違う点に注意**（2026-08-03追記）。上記はMacローカルの
話で、Claude Code on the web 等のクラウドコンテナでは旧
`makechair/spelling-inconsistencies` が `origin`、実体の
`makechair/us-stock-realtime-chart` が `newrepo` として登録された状態で
起動することがある。**正本は常に `us-stock-realtime-chart` の `main`。**
作業前に `git remote -v` と `git log --oneline -3` で現在地を確認すること。

## 4. 保留中・未着手のタスク

### 4-1. Phase 0（日足エンドポイント検証）— 完了

実測値は `docs/analysis-spec.md` 2節に記録済み。API枠を使って再実行しないこと。

### 4-2. Phase 1（日足コーパス構築）

**完了。** main反映、IAM最小権限、unit/timer導入、初回3銘柄、S3 object、
共有REST予算まで本番確認済み。次の定刻実行では新規3銘柄を追加し、既存銘柄は
20時間以上経過したものを古い順に更新する。

### 4-3. Phase 2（Notion取り込み + 統合）

**完了。** ローカル実装と153テスト、SSM/IAM最小権限、systemd timer導入、
Lightsail初回同期まで確認済み。初回同期は368ページを50日付partitionへ正規化し、
50 objectをS3へアップロードして終了コード0。タイマーはenabled/active。

### 4-4. 本体側の未処理TODO

`docs/analysis-spec.md` 7節に既存の一覧あり。SKHY解除と`volume=0`掃除は完了。
残る主な運用作業は、実データを見ながらのpoll設定調整とAlpacaキーの
ローテーション（Alpaca管理画面へのログインが必要）。4分割チャートは
ローカル検証と本番反映まで完了。

### 4-5. Phase 3（Notionイベント × 日足リターン）

**完了・本番稼働中。** `docs/analysis-spec.md` 6節に、反応取引日の決定、
0/1/2/5/20取引日リターン、subsector相対リターン、日またぎ重複の重み付け、
イベント窓の重なり、出力schemaと集計軸を記録し、その仕様に沿ってDuckDB SQL、
テストfixture、Parquet／Markdown／HTMLレポート生成を実装した。
systemd unit配置、初回S3 upload、差分なし再実行まで本番確認済み。

### 4-6. Notion取り込みの改善 ★最優先・未着手

teiten は 2026-08-03 時点で **Haiku 4.5 をやめ、ローカル Qwen3 13B** で
要約している。実行はローカルの **12時・18時 JST の2回**、`MAX_LLM_ITEMS` は
**4→10**（半導体メモリに限らずハイパースケーラも対象にしたため）。
最大要約数は 12件/日 → **20件/日**。

**LLM推論コストが実質ゼロになったため、「token量を増やさない」という
従来の制約は消滅した**（`analysis-spec.md` 5節の旧決定事項は無効。
同節「teiten のローカルQwen移行」が現行）。

代わりに入った制約が本題。**Qwen3 13B は Haiku 4.5 より構造化出力の
スキーマ遵守が弱く、`corpus/news.py` はそれに耐える作りになっていない。**
`normalize_page()`（`news.py:196-232`）は不正な `Tickers` / `EventType` /
`Sentiment` / `Confidence` を1ページでも見つけると `CorpusError` を投げ、
**その日の同期全体を中断する**。1件の不正ページが20件分を落とす。

対処は2方向、両方やるのが望ましい（詳細は `analysis-spec.md` 5節）:

1. **teiten側** — Notionへ書く前にenum正規化・ticker照合（`universe.csv` の
   50銘柄）を通す。ローカル実行なので弾いた記事の再実行コストはゼロ。
   **2026-08-03に実装済みと報告あり（未検証）**
2. **corpus側（このリポジトリ）** — 全断をやめ、不正ページを隔離して
   残りを取り込む。件数と理由を記録すれば「schema driftを黙って混ぜない」
   という当初の意図は保てる。**未着手**

**検証手段は用意済み（2026-08-03）**: `python -m usstocks.corpus.news --check` が
全ページを同じ検証にかけ、止まらずに落ちる分を列挙する。Parquetを書かず
S3も触らないので本番で安全に実行できる。**実行コマンドは
`docs/operations.md`「取り込みが止まったとき（`--check`）」を見ること**
（`/etc/usstocks/usstocks.env` の読み込みが要る。console script
`usstocks-corpus-news` は systemd unit が使っておらず、`current/venv/bin` に
存在しない場合がある）。

**teiten側の修正（`73a79fc`）はコードを読んで検証済み・正しい**が、
**既存ページは直らない**。corpus は毎回全ページを取得するため、Qwen移行後・
この修正前に書かれたページに規約外の値が残っていれば同期は今も止まる。
`--check` を1回流せば確定する（開発コンテナからはNotion認証情報もAWSも
無いため検証できない）。

**Phase 3 の matched 件数が伸びない場合、日足corpusの段階投入の途中だと
決めつける前に、この経路が落ちていないか先に確認する**
（`journalctl -u usstocks-news-corpus.service` に `CorpusError` が出る）。

### 4-7. tickerが付かない362ページ ★次の焦点

2026-08-03の`--check`実測（418ページ）:

| 指標 | 実測 |
|---|---:|
| 規約外で拒否 | 1（隔離実装で解消済み） |
| **tickerが1つも無い** | **362 / 417** |
| `event_type`が空（7/31以前のページ） | 352 |
| ユニバース外ticker | 26種・約35件 |

**価格と突き合わせられるのは約55ページだけ。** Phase 3のmatchedが伸びない
主因は日足corpusの段階投入ではなくこれ。ユニバース外は`SKH`(SK Hynix)、
`SSNLF`(Samsung)、`KIOX`/`KIOXIA`、`POSCO`など韓国・日本のメモリ銘柄が
中心で、teitenの収集源がメモリ寄りである以上は構造的。`SKH`は
`analysis-spec.md` 3節の判断により意図的にユニバース外（Tiingoが
データを返さない）なので永久にマッチしない。

打ち手の候補は`analysis-spec.md` 5節末に3つ挙げてある（tickerが付く率を
上げる／ユニバースを広げる／無tickerイベントをsubsector単位で扱う）。
**どれも未判断。**

## 5. 環境・運用上の注意

- テストは `pytest` で **169件全通過**、ruff も通過（2026-08-03 実測、`a277566`）。
  **`parquet` extra を入れていないと corpus 系10件が `ModuleNotFoundError: pyarrow`
  で落ちる。** 新しい環境ではまず次を実行すること:

  ```bash
  .venv/bin/pip install -e ".[parquet]"   # boto3 / duckdb / pyarrow
  .venv/bin/pytest -q
  ```
- 2026-07-31の本番反映後、collector/API、`usstocks-corpus.timer`、
  `usstocks-news-corpus.timer`はactive。
  poll間隔3変数はenv未指定なので、背景pollはコード既定の3600秒で動く
- 同じ本番確認で、物理メモリ1.9GiB中584MiB使用・available 1.3GiB、
  swap 52KiB、root disk 10%。collector約29.7MiB、API約43.8MiBで余力がある
- 本体は systemd で常時稼働中の想定（`docs/systemd-deployment.md`）。コーパス関連の
  取得処理は**市場が閉じている時間帯（09:00–17:00 JST）に走らせ、ライブポーリングと
  RESTトークンを奪い合わせないこと**（`docs/analysis-spec.md` 4節）
- Tiingo無料枠は 50 calls/hour, 1,000 calls/day, 1 GB/month。配分は
  `docs/analysis-spec.md` 1節の表で確定済み。**月間ユニークシンボル上限は未確認**
  （APIから読めず、ドキュメントも403で取得不可）。50銘柄を一度に投入せず、
  少数ずつ増やして4xxを観測すること
- サブスク枠の節約方針（セッションを短く保つ、`/model sonnet`、Haiku+Batchへの退避等）は
  `docs/analysis-spec.md` 0節を参照。Codex 側でも同様の考え方（長い会話を避ける、
  決定的コードに寄せる）は有効なはず

## 6. よく使うコマンド

```bash
# テスト
.venv/bin/pytest -q

# Phase 0 検証（本番サーバ上で実行する想定のコマンド。docs/analysis-spec.md 2節参照）
sudo -u usstocks env \
  USSTOCKS_TIINGO_API_KEY="$(sudo grep '^USSTOCKS_TIINGO_API_KEY=' \
    /etc/usstocks/usstocks.env | cut -d= -f2-)" \
  /opt/usstocks/current/venv/bin/python \
  /opt/usstocks/app/scripts/probe_tiingo_daily.py

# ローカル開発（偽データ）
make install && make dev   # http://127.0.0.1:8000
```

## 7. 次に着手するならこの順で

1. **【最優先】Notion取り込みの改善（§4-6）** — Qwen3 13Bの出力ゆれで
   同期が全断しうる。まず `journalctl -u usstocks-news-corpus.service` で
   `CorpusError` が出ていないか確認し、出ていれば corpus 側の隔離実装と
   teiten 側の正規化を進める
2. 次回の日足timer後、Phase 3のmatched数が段階的に増えることを確認
   （伸びない場合は1を先に疑う）
3. **次回timerで新規3銘柄追加と共有REST予算を確認**（4xxなら増加を止める）
4. 分析レポートの内容拡充とQwen考察の調整（§2-B）— 重要だが1より後
5. Alpaca APIキーを管理画面でローテーション
6. 実測した通信量と空fetch警告を見ながらpoll間隔を調整

新しい担当者・エージェントが作業を始める際は、まず `docs/analysis-spec.md` の
「再調査してはいけない確定事項」を読み、同じ検証をやり直さないこと。作業後は
このファイル（`docs/handoff.md`）を更新し、次の引き継ぎに備えること。
