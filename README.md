# 個人向けリアルタイム米国株チャートシステム

保有する米国株の1分足を常時収集し、本人だけがアクセスできるWebチャートで表示するシステム。

- **市場データ:** Tiingo（WebSocket + REST）／障害時の予備に Alpaca IEX
- **バックエンド:** Python 3.11 / FastAPI、収集プロセスは独立した asyncio プロセス
- **データベース:** SQLite（WAL）
- **ブラウザ配信:** SSE（最大1秒間隔）
- **フロントエンド:** TradingView Lightweight Charts（ビルド不要・同梱）
- **外部入口・認証:** Cloudflare Tunnel + Cloudflare Access（Google認証）
- **想定インフラ:** Amazon Lightsail 1GB

## 最初に読むもの

**[docs/spec-review.md](docs/spec-review.md) — 仕様書の妥当性レビュー。**

構成の骨格は妥当だが、実装前に潰すべき前提が4件、未定義の設計判断が9件あった。本実装はそれらに対する決定を含んでいる。特に重要な2点:

- **A-1 帯域**: 無料枠の 1GB/月 は、仕様書の30銘柄では最も楽観的な仮定でも使い切る。**この指摘を受けて対象を最大10銘柄へ引き下げた**（`USSTOCKS_MAX_SYMBOLS=10`）。10銘柄なら中程度の流動性で枠内に収まる見込みだが、高流動性銘柄ばかりなら依然超過しうるため、Collector の実測メーター（`/api/health`）でフェーズ1に必ず確認すること。
- **A-2 RESTレート**: 50回/時では、再接続のたびに全銘柄を補完すると枯渇する。10銘柄でも不安定な朝には超えるため、永続トークンバケットと要求の合体で対処した。

## クイックスタート（ローカル、認証なし・偽データ）

```bash
make install
make dev
# http://127.0.0.1:8000
```

`make dev` は疑似データ（`source=mock`）で collector と API を両方起動する。**画面に出る価格はすべて架空**であり、DBの `source` 列にも `mock` と記録されるため実データと混ざらない。認証を無効にできるのはループバック宛のときだけで、それ以外のアドレスでは起動を拒否する。

## 実データで動かす

```bash
cp .env.example .env
# USSTOCKS_TIINGO_API_KEY を設定し、USSTOCKS_PRIMARY_SOURCE=tiingo にする
make migrate
.venv/bin/python scripts/seed.py AAPL MSFT   # 監視銘柄を登録
make dev-collector    # 別ターミナルで make dev-api
```

## 構成

```
Tiingo WebSocket / REST
        |
        v
  collector  ──書き込み──>  SQLite (market.db)  <──読み取り──  api
   |  WS常時接続                bars_1m 等                    |  FastAPI
   |  1分足生成                                               |  SSE配信
   |  再接続・欠損補完      live.db (tmpfs)                   |  静的フロント
   └──────────書き込み───>  現在値・現在足   ──読み取り───────┘
                                                              |
                                                        cloudflared
                                                              |
                                              Cloudflare Tunnel / Access
                                                              |
                                                  https://stocks.example.com
```

`collector` と `api` は別プロセスで、個別に再起動できる。時系列テーブルへ書くのは collector だけ。api はウォッチリストの小テーブルだけを書く（[spec-review A-3](docs/spec-review.md) 参照）。

## API

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/api/symbols` | 登録銘柄一覧（`?watched_only=true`） |
| GET | `/api/symbols/search?q=` | ティッカー／企業名検索 |
| PUT | `/api/symbols/{symbol}` | ウォッチリスト登録・更新 |
| DELETE | `/api/symbols/{symbol}` | 購読解除（履歴は保持） |
| GET | `/api/bars/{symbol}?days=1` | 1分足履歴 |
| GET | `/api/live?symbols=A,B` | SSEストリーム |
| GET | `/api/live/snapshot` | 現在値（ポーリング用） |
| GET | `/api/export/csv?symbols=A` | CSVエクスポート |
| GET | `/api/export/parquet?symbols=A` | Parquet（`.[parquet]` 導入時） |
| GET | `/api/health` | 稼働・接続・鮮度・帯域・RESTレート残量 |
| GET | `/api/ping` | 認証付き疎通確認 |
| GET | `/api/livez` | プロセス生存確認（唯一の無認証エンドポイント） |

`/api/livez` 以外はすべて、静的ファイルとSSEを含めて認証が必要。

## デプロイ

AWS 側は **Terraform** で管理する（[docs/aws-deployment.md](docs/aws-deployment.md)）。SAM/CloudFormation は Lightsail リソースを持たないため使えない。

```bash
scripts/bootstrap-tf-state.sh dev01          # 初回のみ: state バケット作成
cd infra/terraform
terraform init -backend-config=backend.hcl
terraform apply -var-file=dev01.tfvars       # profile=dev01
```

Terraform の管理対象は Lightsail（インスタンス、公開ポート、静的IP、自動スナップショット）、S3バックアップ、IAM、SNS、CloudWatch、Budgets。

`main` へ push すると GitHub Actions が **テスト → `terraform apply`** を実行し、AWS 側の設定値を反映する。アプリのコード自体はインスタンス側の pull 型エージェント（`deploy/agent/`）が2分間隔で取得する。ランナーの送信元IPが固定されないため、SSH 配布にすると22番をインターネットに開ける必要があり、閉域設計と矛盾するためである。

手動で起動する場合、または Terraform を使わない場合は [docs/deployment.md](docs/deployment.md) を参照。

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Lightsail のファイアウォールは **SSHのみ許可**でよい。Cloudflare Tunnel は外向き接続だけを使うため、80/443 を開ける必要がない。Terraform 側でも SSH 以外は明示的に閉じている。

## 運用

[docs/operations.md](docs/operations.md) に、日次バックアップ、リストア検証、フェーズ1の実測チェックリスト、監視項目をまとめている。

```bash
deploy/backup/backup.sh                              # 整合バックアップ + S3
deploy/backup/restore.sh s3://bucket/... /tmp/x.db   # リストアと検証
```

## 開発

```bash
make test     # pytest
make lint     # ruff
```

## ライセンスと利用条件

- 本リポジトリのコード: LICENSE を参照。
- 同梱の Lightweight Charts: Apache License 2.0（`web/vendor/LICENSE-lightweight-charts.txt`）。TradingView への帰属表示を画面下部に常設している。
- **市場データ**: Tiingo / Alpaca の利用条件（個人内部利用）に従うこと。認証を付けても再配信ライセンスが緩和されるわけではない。第三者への公開へ移行する場合は契約を再確認すること（仕様書 4.2）。
