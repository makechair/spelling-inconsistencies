# デプロイ手順（手動）

リポジトリ: <https://github.com/makechair/us-stock-realtime-chart>

> **Terraform を使う場合はこの文書ではなく [aws-deployment.md](aws-deployment.md) を参照。**
> インスタンス作成・ポート設定・S3・IAM は Terraform が行い、アプリの配置は
> pull 型エージェントが行う。本文書は、IaC を使わず手で構築する場合、および
> Terraform 適用後にインスタンス内部で何が起きているかを確認する場合の手順である。


仕様書13章のフェーズ2に対応する。Lightsail 1GB を前提とするが、Docker が動く
任意のLinuxで同じ手順が使える。

## 0. 事前準備

- Tiingo アカウントとAPIキー
- Cloudflare アカウント（Zero Trust を有効化）
- ドメイン（登録先は任意、権威DNSをCloudflareへ委任する）
- S3バケット（バックアップ用）

## 1. Lightsail インスタンス

1. Ubuntu LTS、1GB RAM / 40GB SSD プランを作成する。
2. **ファイアウォールは SSH(22) だけを許可**する。80/443 は開けない。
   Cloudflare Tunnel は外向き接続しか使わないため不要である（仕様書4.5）。
3. 静的IPを割り当てる（SSH用。DNSには使わない）。

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-v2 sqlite3 unattended-upgrades
sudo systemctl enable --now docker
sudo dpkg-reconfigure -plow unattended-upgrades   # 仕様書4.5: 自動セキュリティ更新
sudo usermod -aG docker "$USER" && newgrp docker
```

### スワップ

1GB機で Docker を3コンテナ動かすとメモリが逼迫しうる（仕様書12「1GBメモリ不足」）。
スワップを用意しておくと、OOMで収集が落ちる代わりに一時的に遅くなるだけで済む。

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Cloudflare Tunnel と Access

> **既に別用途で使っているドメインに相乗りする場合は
> [cloudflare-setup.md](cloudflare-setup.md) を参照。**
> 権威DNSの移行を伴うため、既存サイトを落とさない手順が必要になる。

### 2.1 DNSの委任

ドメインをCloudflareに追加し、レジストラ側のネームサーバーをCloudflareへ向ける。
Route 53 でドメインを購入したまま権威DNSだけCloudflareにする構成でよい。
**ACM証明書とRoute 53 Hosted Zone は不要**である（仕様書7.3）。

### 2.2 Tunnel の作成

1. Zero Trust ダッシュボード → Networks → Tunnels → Create a tunnel（Cloudflared）。
2. トークンをコピーし、`.env` の `CLOUDFLARE_TUNNEL_TOKEN` に設定する。
3. Public hostname を追加する:
   - Subdomain: `stocks`、Domain: 自分のドメイン
   - Service: `HTTP` → `api:8000`（Docker Compose の場合。systemdなら `http://127.0.0.1:8000`）

### 2.3 Access アプリケーション

1. Zero Trust → Access → Applications → Add → Self-hosted。
2. Application domain: `stocks.example.com`
3. **Policy: Allow — Emails — 自分のアドレスだけ**。
   既定拒否のままにし、Bypass ポリシーは作らない（仕様書4.5）。
4. Identity provider に Google を設定する。
5. **Application Audience (AUD) Tag をコピーする。** アプリ側の検証に必須である。

> Access の設定だけに依存しない。アプリ側でもJWTを検証しているので、
> ポリシーを消してしまっても即座に公開状態にはならない
> （[spec-review A-4](spec-review.md)）。

## 3. アプリケーションの配置

```bash
sudo mkdir -p /opt/usstocks && sudo chown "$USER" /opt/usstocks

# /opt/usstocks/app が配置先。Terraform 経由のデプロイエージェント
# （deploy/agent/）も同じパスを使うので、後から自動デプロイへ移行しても
# クローンが二重にならない。
git clone git@github.com:makechair/us-stock-realtime-chart.git /opt/usstocks/app && cd /opt/usstocks/app
cp .env.example .env
chmod 600 .env
```

`.env` を編集する。最低限必要なもの:

```ini
USSTOCKS_PRIMARY_SOURCE=tiingo
USSTOCKS_TIINGO_API_KEY=<キー>

USSTOCKS_AUTH_MODE=cloudflare_access
USSTOCKS_CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
USSTOCKS_CF_ACCESS_AUD=<AUDタグ>
USSTOCKS_ALLOWED_EMAILS=you@example.com

CLOUDFLARE_TUNNEL_TOKEN=<トークン>
USSTOCKS_BACKUP_S3_URI=s3://your-bucket/usstocks
```

起動:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f collector
```

監視銘柄を登録する:

```bash
docker compose -f deploy/docker-compose.yml exec api \
  python scripts/seed.py AAPL MSFT NVDA
```

`https://stocks.example.com` を開くとGoogle認証を求められ、通過後に画面が出る。

### 動作確認

```bash
# 認証前は到達できないこと（仕様書3.5）
curl -sI https://stocks.example.com | head -1        # -> 302 (Access へ)

# 別のGoogleアカウントで開くと拒否されること
# 収集状況
docker compose -f deploy/docker-compose.yml exec api \
  python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/livez')))"
```

## 4. systemd を使う場合

Docker を使わない構成。どちらか一方を選ぶこと。

```bash
sudo useradd --system --home /var/lib/usstocks --create-home usstocks
sudo mkdir -p /etc/usstocks && sudo cp .env /etc/usstocks/usstocks.env
sudo chown root:usstocks /etc/usstocks/usstocks.env && sudo chmod 640 /etc/usstocks/usstocks.env

cd /opt/usstocks/app
python3 -m venv .venv && .venv/bin/pip install .
sudo chown -R usstocks:usstocks /opt/usstocks /var/lib/usstocks

sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usstocks-collector usstocks-api usstocks-backup.timer
```

cloudflared は公式パッケージで導入し、`deploy/cloudflared/config.example.yml`
を `/etc/cloudflared/config.yml` に置き換えて `service` に origin
`http://127.0.0.1:8000` を設定する。

プロセスを個別に再起動できること（仕様書3.6）:

```bash
sudo systemctl restart usstocks-api        # 収集は止まらない
sudo systemctl restart usstocks-collector
```

## 5. バックアップ

```bash
# AWS CLI は apt では入らない。Ubuntu 24.04 は awscli(v1) をアーカイブから
# 削除しており、v2 はそもそもパッケージ化されていない
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
sudo apt install -y unzip && unzip -q -o /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install --update
aws --version
aws configure    # バケットへの s3:PutObject 権限だけを持つIAMユーザーが望ましい
sudo mkdir -p /var/backups/usstocks && sudo chown usstocks /var/backups/usstocks
```

systemd タイマーが日次で `deploy/backup/backup.sh` を実行する。Docker Compose
構成では、ホスト側の cron から同じスクリプトを叩く:

```cron
10 7 * * * cd /opt/usstocks/app && ./deploy/backup/backup.sh >> /var/log/usstocks-backup.log 2>&1
```

S3バケット側にライフサイクルルールを設定する（仕様書10.4）:

- `daily/` を30日で削除、または Glacier Instant Retrieval へ移行
- バージョニングは不要（毎回別キーで置くため）

## 6. 請求アラート（仕様書4.1）

AWS Budgets で月次予算（例: 12米ドル）とアラートを設定する。Lightsail は固定
料金だが、S3とデータ転送は従量である。

## 7. 監視

`/api/health` は認証が必要なので、外形監視には使えない。日次で自分で開くか、
Lightsail のメトリクスアラーム（CPU、バーストキャパシティ）と、以下の cron を
併用する。

```bash
# 収集が止まっていたら通知する例
*/15 * * * * docker compose -f /opt/usstocks/app/deploy/docker-compose.yml exec -T api \
  python -c "
import json,sys,urllib.request
h=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/livez'))
" || echo 'usstocks api down' | mail -s alert you@example.com
```

詳細な監視項目は [operations.md](operations.md) を参照。

## 8. 更新

```bash
cd /opt/usstocks/app && git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

マイグレーションは各プロセスの起動時に自動適用される。API だけを更新しても
収集は継続する。
