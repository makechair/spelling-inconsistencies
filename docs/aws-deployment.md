# AWS構成のIaC管理とデプロイ

Lightsail を含む AWS 側の構成を Terraform で管理し、`main` への push で自動反映する。

## 0. なぜ SAM ではなく Terraform か

当初 AWS SAM の採用を検討したが、**SAM（CloudFormation）は Lightsail を管理できない**。CloudFormation には `AWS::Lightsail::*` に相当するリソースタイプが存在せず、Lightsail インスタンスを IaC 化する手段がない。

さらに SAM の主対象である Lambda は、常時 WebSocket 接続とローカル SQLite 永続化という本システムの実行モデルに合わない。これは [spec-review C-3](spec-review.md) で Cloud Run を不採用とした理由と同じである。

Terraform の AWS プロバイダには `aws_lightsail_instance` 等が揃っており、加えて S3・IAM・SNS・Budgets も同じツールで管理できる。IaC ツールを1つに保てるため、Terraform を採用した。

| 管理対象 | ツール | 備考 |
|---|---|---|
| Lightsail インスタンス、ポート、静的IP、自動スナップショット | Terraform | CloudFormation では不可能 |
| S3 バックアップバケット、ライフサイクル | Terraform | |
| IAM（バックアップ用ユーザー、CI用ロール） | Terraform | |
| SNS、CloudWatch アラーム、Budgets | Terraform | |
| アプリケーションコードの配置 | Pull型エージェント | 後述。Terraform の管轄外 |
| Cloudflare Tunnel / Access | 手動（Cloudflare側） | AWS 外 |

## 1. 前提

- AWS CLI プロファイル **`dev01`** が設定済みであること
- Terraform 1.6 以上（CI は 1.15.8 を使用）
- リポジトリ: `makechair/us-stock-realtime-chart`

```bash
aws sts get-caller-identity --profile dev01   # 疎通確認
```

## 2. 初回のみ: state 用バケットの作成

Terraform は自分の state を置くバケットを自分では作れないので、1度だけ手で作る。

```bash
scripts/bootstrap-tf-state.sh dev01 ap-northeast-1
```

バージョニング・暗号化・パブリックアクセス遮断・古いバージョンの90日削除まで設定される。state はバックアップと違い**上書き更新**されるため、バージョニングは必須である（壊れた書き込みからの復旧経路がこれしかない）。

## 3. 初回の apply（ローカル、profile=dev01）

```bash
cd infra/terraform
cp backend.hcl.example backend.hcl
cp dev01.tfvars.example dev01.tfvars
```

`dev01.tfvars` を編集する。最低限:

```hcl
alert_email       = "you@example.com"
tf_state_bucket   = "usstocks-tfstate-dev01-<アカウントID>"
ssh_allowed_cidrs = ["<自宅のグローバルIP>/32"]
```

> `ssh_allowed_cidrs` に既定値は用意していない。未設定なら plan が失敗する。`0.0.0.0/0` を入れると SSH がインターネットに開放され、閉域設計（仕様書4.5）が崩れる。

```bash
terraform init -backend-config=backend.hcl
terraform plan  -var-file=dev01.tfvars
terraform apply -var-file=dev01.tfvars
```

`dev01.tfvars` と `backend.hcl` は gitignore 済み。自宅IPをリポジトリに入れないための措置である。

### apply 後に出る output

```bash
terraform output
```

| output | 用途 |
|---|---|
| `instance_public_ip` | SSH 先。ブラウザはここではなく Cloudflare 経由で来る |
| `open_ports` | SSH のみ開いていることの確認 |
| `backup_s3_uri` | インスタンスの `USSTOCKS_BACKUP_S3_URI` に設定 |
| `backup_uploader_user_name` | このユーザーのアクセスキーを作る（次項） |
| `github_deploy_role_arn` | GitHub の変数 `AWS_DEPLOY_ROLE_ARN` に設定 |

## 4. アクセスキーの発行（Terraform 管理外）

バックアップ用の IAM ユーザーは Terraform が作るが、**アクセスキーは意図的に作らせていない**。`aws_iam_access_key` を使うと**シークレットが state ファイルに平文で残る**ためである。state バケットとインスタンスでは侵害範囲が異なるので、鍵をそこへ置きたくない。

```bash
aws iam create-access-key \
  --user-name "$(terraform output -raw backup_uploader_user_name)" \
  --profile dev01
```

出力された鍵をインスタンスの `/etc/usstocks/usstocks.env` に書く。権限は `daily/` への `s3:PutObject` のみで、読み取りも削除もできない。鍵が漏れても蓄積した履歴は読み出せない。

ローテーション（四半期ごと、仕様書4.5）:

```bash
aws iam create-access-key --user-name <user> --profile dev01   # 新しい鍵を作る
# インスタンスの env を更新し、バックアップを1回走らせて成功を確認してから
aws iam delete-access-key --user-name <user> --access-key-id <古い鍵> --profile dev01
```

## 5. インスタンスの初期設定

`user_data` はホストの準備までしか行わない。**アプリのコードも秘密情報も user_data には入れていない** — user_data はメタデータサービスから読めるため、秘密情報を置いてはいけない（仕様書4.5）。

SSH で入って以下を行う。

```bash
ssh ubuntu@$(terraform output -raw instance_public_ip)

# 1. 実際の値を書く
sudo vi /etc/usstocks/usstocks.env

# 2. リポジトリ読み取り専用のデプロイキーを置く
sudo -u usstocks ssh-keygen -t ed25519 -N '' -f /var/lib/usstocks/.ssh/id_ed25519
sudo cat /var/lib/usstocks/.ssh/id_ed25519.pub
#    → GitHub の Settings > Deploy keys に「Read only」で登録
sudo -u usstocks ssh-keyscan github.com >> /var/lib/usstocks/.ssh/known_hosts

# 3. デプロイエージェントを有効化
sudo cp /opt/usstocks/app/deploy/agent/usstocks-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usstocks-deploy.timer
```

## 6. GitHub 側の設定

リポジトリの Settings > Secrets and variables > Actions > **Variables**（Secrets ではない。いずれも秘密情報ではないため）:

| 変数 | 値 |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `terraform output -raw github_deploy_role_arn` |
| `TF_STATE_BUCKET` | state バケット名 |
| `ALERT_EMAIL` | 通知先アドレス |
| `SSH_ALLOWED_CIDRS` | `["203.0.113.10/32"]`（JSON配列の文字列） |
| `CREATE_OIDC_PROVIDER` | 初回 apply 済みなら `false` |

`CREATE_OIDC_PROVIDER` は既定 `false` にしてある。OIDC プロバイダはアカウントに1つしか作れず、ローカルの初回 apply で作成済みのため、CI が再作成しようとすると `EntityAlreadyExists` で失敗する。

## 7. デプロイの流れ

```
main へ push
    │
    ├─ GitHub Actions: test            （ruff + pytest 103件）
    ├─ GitHub Actions: terraform-check （fmt + validate。認証情報不要）
    │        │ どちらか失敗したらここで停止し、AWS へは一切触れない
    │        v
    └─ GitHub Actions: apply
             │ OIDC でロールを引き受ける（保存された鍵はゼロ）
             │ AWS 側の設定値（Lightsail、S3、IAM、SNS、Budgets）を反映
             v
       インスタンス側: usstocks-deploy.timer が2分ごとに main を確認
             │ 変化があれば git pull → docker compose up -d --build
             │ /api/livez が応答するまで確認して完了
             v
       collector / api が新リビジョンで稼働
```

Pull Request で実行されるのは **テストと `terraform validate` まで**で、`plan` は走らない。

これは制限ではなく、信頼ポリシーとの整合である。デプロイロールは `sub=repo:OWNER/REPO:ref:refs/heads/main` に固定しており、PR の OIDC トークンは `sub=repo:OWNER/REPO:pull_request` を提示するため、**PR からは AssumeRole できない**。`pull_request` を信頼対象に加えれば plan は動くが、PR を出せる立場の誰もが AWS の読み取り資格情報を得ることになる。個人利用では割に合わないため、狭い信頼を維持した。

PR で plan を見たい場合は、読み取り専用の別ロール（`pull_request` を信頼し、権限は `Describe*` / `Get*` / `List*` のみ）を追加するのが正しい拡張である。

### なぜアプリのデプロイを push 型（SSH）にしないのか

GitHub Actions のランナーは**送信元IPが固定されない**。SSH で配布するには 22番を `0.0.0.0/0` に開ける必要があり、Terraform で明示的に閉じている閉域設計（仕様書3.5、4.5）と正面から矛盾する。

インスタンス側から2分ごとに `git fetch` する方式なら、通信はすべて外向きで完結する。Cloudflare Tunnel と同じ考え方である。デプロイの遅延は最大2分だが、個人利用では問題にならない。

なお、**変化がなければ何もしない**。無条件に再起動すると WebSocket が切れ、再接続のたびに REST の補完枠を消費する（[spec-review A-2](spec-review.md)）。

## 8. 日常の操作

```bash
cd infra/terraform

# 現状との差分確認
terraform plan -var-file=dev01.tfvars

# インスタンスのプランを 2GB へ変更（仕様書12「1GBメモリ不足」への対応）
#   注意: bundle_id の変更は Lightsail では再作成になる。事前にバックアップを
#   取得し、リストア手順（docs/operations.md）を確認すること
terraform plan -var-file=dev01.tfvars -var='lightsail_bundle_id=medium_3_0'

# SSH 許可元の変更（引っ越し・回線変更時）
terraform apply -var-file=dev01.tfvars -var='ssh_allowed_cidrs=["198.51.100.5/32"]'
```

### 保護してあるリソース

| リソース | 保護 | 理由 |
|---|---|---|
| S3 バックアップバケット | `prevent_destroy = true` | 蓄積した1分足の唯一の外部コピー。`terraform destroy` で消えてはいけない |
| Lightsail の `user_data` | `ignore_changes` | bootstrap を編集しただけでインスタンスが再作成され、DBが消えるのを防ぐ |

インスタンスを意図的に作り直す場合は、先にバックアップを取り、リストア手順（[operations.md](operations.md) 3章）に従う。

## 9. 検証状況

この環境では AWS 認証情報がないため、**実際の `terraform apply` は実行していない**。実施済みなのは以下まで:

- `terraform fmt -check` — 整形確認
- `terraform init` — AWS プロバイダ 6.56.0 を取得
- `terraform validate` — **実プロバイダのスキーマに対して検証済み**（リソース名・属性名・型の誤りは検出される）

`terraform plan` はアカウントへの API 呼び出しを伴うため未実施である。したがって、実際に apply する前に必ず `terraform plan` の出力を目視すること。特に次の2点は plan でしか判明しない。

- `lightsail_availability_zone` が `aws_region` 内に実在するか
- `create_github_oidc_provider = true` が既存プロバイダと衝突しないか

## 10. コストへの影響

Terraform が追加するのは、仕様書の想定（月10米ドル程度）に対して次のとおり。

| リソース | 月額目安 |
|---|---|
| Lightsail `small_3_0` | 仕様書記載の固定額 |
| 静的IP（インスタンスに接続中） | 0米ドル。**未接続だと課金される**ので、インスタンスを削除するなら静的IPも削除する |
| S3 バックアップ（数GB、STANDARD_IA、30世代） | 1米ドル未満 |
| S3 state バケット | 無視できる |
| Lightsail 自動スナップショット | ディスク使用量に応じた従量。40GBのうち実使用分のみ |
| SNS（メール数通） | 実質0 |
| CloudWatch アラーム 1個 | 無料枠内（10個まで） |
| Budgets | 2予算まで無料 |

Budgets を月12米ドルに設定し、実績80%と予測100%で通知する。予測通知は「使い切る前」に気づける唯一の手段である。
