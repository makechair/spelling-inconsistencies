# 引き継ぎドキュメント（Claude → 次の担当AI/人間）

作成日: 2026-07-31。Claude(Opus/Sonnet 5) の利用上限が近いため、OpenAI Codex へ
作業を引き継ぐために作成。**このファイルはリポジトリに残す前提で書いている
（消さないこと）。作業が進んだら追記・更新して次の引き継ぎに使ってよい。**

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

コミット履歴（このセッション分、新しい順）:

```
ee16839 Complete Notion corpus and data-quality hardening [skip ci]
47a04ee Turn the teiten open questions into decisions
9ee7c56 Replace the teiten integration guess with what the code actually does
4822043 Write down what a fresh session needs so it does not re-derive it
```

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
ローテーション（Alpaca管理画面へのログインが必要）。

## 5. 環境・運用上の注意

- テストは `pytest` で153件全通過（Codex引き継ぎ後）。`.venv/bin/pytest -q` で確認可能
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

1. **次回timerで新規3銘柄追加と共有REST予算を確認**（4xxなら増加を止める）
2. Alpaca APIキーを管理画面でローテーション
3. 実測した通信量と空fetch警告を見ながらpoll間隔を調整

新しい担当者・エージェントが作業を始める際は、まず `docs/analysis-spec.md` の
「再調査してはいけない確定事項」を読み、同じ検証をやり直さないこと。作業後は
このファイル（`docs/handoff.md`）を更新し、次の引き継ぎに備えること。
