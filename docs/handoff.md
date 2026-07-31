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

このセッションは**設計・意思決定のみ**で、コーパス構築のコードはまだ1行も書いていない
（Phase 0 未実行）。実施したのは:

1. `docs/analysis-spec.md` を新規作成 — Phase 0〜4の作業計画、REST枠の配分、
   50銘柄ユニバース案（`data/universe.csv` はまだ存在しない）、S3ストレージ設計
2. teiten-pipeline との統合方針を、**あちら側の会話で実装コードを確認してもらった上で**
   確定（`docs/analysis-spec.md` 5節）。要点:
   - 永続化は **Notion API 経由で読み出す**方式に決定（S3直書きは不採用）
   - teiten 側で `tickers`/`event_type`/`sentiment`/`confidence` を追加し、
     Notionプロパティ `Tickers`/`EventType`/`Sentiment`/`Confidence` として書込済み
     （teiten側で実装・デプロイ完了、2026-07-31 02:40 UTC）
   - teiten 側は収集セクターを `ai_platform`/`eda_ip` まで拡大済み、
     Haiku呼び出しコストは変えていない（セクター最低枠保証で対応）
3. Phase 0（`scripts/probe_tiingo_daily.py` で日足の `adjClose`/`splitFactor`/`divCash`
   有無を検証）は**まだ実行していない**。これが次の一歩

コミット履歴（このセッション分、新しい順）:

```
47a04ee Turn the teiten open questions into decisions
9ee7c56 Replace the teiten integration guess with what the code actually does
4822043 Write down what a fresh session needs so it does not re-derive it
```

## 3. リポジトリ／ブランチの実態（要注意・ハマりどころ）

このセッション中に発覚した事実:

- **`origin`**（`makechair/spelling-inconsistencies`）は**移行前の旧リポジトリ**で、
  現在のブランチ `claude/us-stock-realtime-chart-xpdow8` は Terraform/AWS周りの
  古いコミットで止まっている。
- **実体は `makechair/us-stock-realtime-chart`**（このローカルリポジトリでは git remote
  `newrepo` として登録されている）。2026-07-28のコミット
  "Point the docs at the new repository and unify the install path" で移行が明記されている。
- このセッションでの作業はすべて `newrepo`（`makechair/us-stock-realtime-chart`）の
  `main` ブランチと `claude/us-stock-realtime-chart-xpdow8` ブランチ両方に push 済み。
  **`origin`（spelling-inconsistencies）にも同じコミットを push しているが、そちらは
  本来のプロジェクトの置き場ではない**可能性が高い。次の担当者は、まず
  `makechair/us-stock-realtime-chart` が正しい push 先であることを確認してから作業すること。
- ローカルの `.git/config` にはこの2つの remote が両方登録されており、ブランチの
  tracking先が食い違って local HEAD が意図せず古いコミットに戻る事故が過去に一度発生した
  （fast-forward で復旧済み、データ損失なし）。ブランチを切り替える前に
  `git log --oneline -3` で現在地を確認する習慣を推奨。

## 4. 保留中・未着手のタスク

### 4-1. Phase 0（日足エンドポイント検証）★次にやるべきこと

`docs/analysis-spec.md` 2節のコマンドで `scripts/probe_tiingo_daily.py` を実行し、
`adjClose`/`splitFactor`/`divCash` が揃っているか確認する。判定基準・次の行動も
同節に記載済み。銘柄を列挙しない設計（ユニークシンボル枠を温存するため）なので、
そのまま実行すればよい。

### 4-2. Phase 1（日足コーパス構築）

Phase 0 通過後。`data/universe.csv` の作成（`docs/analysis-spec.md` 3節の下書きを
編集して確定）、取得スクリプト、systemd timer、S3 Parquet 書き出しが成果物。

### 4-3. Phase 2（Notion取り込み + 統合）

teiten 側の実装は完了済みなので、**このリポジトリ側で Notion API から
`Tickers`/`EventType`/`Sentiment`/`Confidence` を含む既存フィールドを読み出す
スクリプトを新規実装する**必要がある。Notion の DB ID・トークンは teiten 側の
SSM (`/teiten/notion-token`, `/teiten/notion-db-id`) にあるはずだが、**このリポジトリ
からは未確認**。teiten 側との接続テストがまだ行われていない（`docs/analysis-spec.md`
5節「残課題」参照）。

### 4-4. 本体側の未処理TODO

`docs/analysis-spec.md` 7節に既存の一覧あり（`background_poll_seconds` の変更、
SKHY削除、`volume=0` の掃除、Alpacaキーのローテーション等）。コーパス作業とは独立に
対応可能。

## 5. 環境・運用上の注意

- テストは `pytest` で130件全通過（この引き継ぎ時点）。`.venv/bin/pytest -q` で確認可能
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

1. **Phase 0 検証**（§4-1）— まだ誰も実行していない、一番手前のタスク
2. **teiten側とのNotion API接続確認**（§4-3）— 実際にスキーマが読み出せるか
3. **`data/universe.csv` の確定**（§4-2）— Phase 0 通過後、3節の下書きを編集
4. 本体側の未処理TODO（§4-4）— 余力があれば、コーパス作業と並行可能

新しい担当者・エージェントが作業を始める際は、まず `docs/analysis-spec.md` の
「再調査してはいけない確定事項」を読み、同じ検証をやり直さないこと。作業後は
このファイル（`docs/handoff.md`）を更新し、次の引き継ぎに備えること。
