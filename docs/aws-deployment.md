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

- AWS CLI が設定済みであること（プロファイル名は既定で `default`）
- **実行する IAM ユーザーに Lightsail / Budgets / IAM の権限があること**

  権限が足りないと apply が途中で止まる。18/21 まで作成されてから
  `lightsail:CreateInstances` で失敗する、という形になる。必要な権限は
  `infra/iam/terraform-operator-policy.json` にまとめてあるので、初回の
  apply 前に自分のユーザーへ付与する。

  ```bash
  ME=$(aws sts get-caller-identity --query Arn --output text | sed 's|.*/||')
  aws iam put-user-policy \
    --user-name "$ME" \
    --policy-name usstocks-terraform-operator \
    --policy-document file://infra/iam/terraform-operator-policy.json
  ```

  詳細は [infra/iam/README.md](../infra/iam/README.md) を参照。
- Terraform **1.10 以上**（`use_lockfile` に必要。CI は 1.15.8 を使用）

  macOS で未導入なら HashiCorp 公式 tap から入れる。Homebrew の
  `brew install terraform` は配布元が変わることがあるため、公式 tap を使う。

  ```bash
  brew tap hashicorp/tap
  brew install hashicorp/tap/terraform
  terraform version          # v1.10 以上であること

  # 既に入っていて古い場合
  brew upgrade hashicorp/tap/terraform
  ```

  > OpenTofu（`tofu`）は概ね互換だが、`use_lockfile` は Terraform 1.10 で
  > 追加された設定のため、そのままでは backend 初期化に失敗しうる。
  > 併用する場合は backend の locking 設定を読み替えること。

  Homebrew が使えない場合（Xcode Command Line Tools の更新待ちなど）は、
  公式バイナリを直接置けばよい。ビルドツールは不要である。

  ```bash
  VER=1.15.8
  A=$([ "$(uname -m)" = "arm64" ] && echo arm64 || echo amd64)
  curl -fLO "https://releases.hashicorp.com/terraform/${VER}/terraform_${VER}_darwin_${A}.zip"
  unzip -o "terraform_${VER}_darwin_${A}.zip"
  # ダウンロードした実行ファイルは Gatekeeper の検疫属性が付くので外す
  xattr -d com.apple.quarantine terraform 2>/dev/null || true
  sudo mv terraform /usr/local/bin/
  terraform version
  ```

### ローカルに入れずに済ませる方法

Terraform をローカルへ入れるのは**必須ではない**。ただし**初回の apply だけは
CI では実行できない**。CI は OIDC ロールを引き受けて認証するが、そのロール自体
がこのスタックで作られるためである（鶏と卵）。したがって「AWS 認証情報を持つ
どこか」で1度だけ動かす必要がある。それがローカルである必要はない。

| 方法 | 向き不向き |
|---|---|
| ローカルに導入 | 差分を随時 `plan` で確認したいなら最も快適 |
| **AWS CloudShell** | ブラウザだけで完結。既に認証済みで、Mac 側に何も入れなくてよい |
| Docker | `docker run --rm -v "$PWD:/w" -w /w -v ~/.aws:/root/.aws hashicorp/terraform:1.15 plan` |

**初回 apply さえ終われば、以降は `main` への push で GitHub Actions が
`terraform apply` を実行する**ので、ローカルの Terraform は任意になる。

CloudShell を使う場合は、リポジトリが private なのでクローンに認証が必要になる。
`gh auth login` でトークンを作るか、`infra/terraform` 配下のファイルだけを
コピーしてもよい（`terraform.tfvars` と `backend.hcl` はそこで作る）。
- リポジトリ: <https://github.com/makechair/us-stock-realtime-chart>（private）

```bash
aws sts get-caller-identity   # 疎通確認。Arn に自分のIAMユーザー名が出る
```

## 2. 初回のみ: state 用バケットの作成

Terraform は自分の state を置くバケットを自分では作れないので、1度だけ手で作る。

```bash
scripts/bootstrap-tf-state.sh default ap-northeast-1
```

バージョニング・暗号化・パブリックアクセス遮断・古いバージョンの90日削除まで設定される。state はバックアップと違い**上書き更新**されるため、バージョニングは必須である（壊れた書き込みからの復旧経路がこれしかない）。

## 3. 初回の apply（ローカル）

```bash
cd infra/terraform
cp backend.hcl.example backend.hcl
cp terraform.tfvars.example terraform.tfvars
```

`terraform.tfvars` を編集する。最低限:

```hcl
aws_profile     = "default"          # ~/.aws/config のプロファイル名
alert_email     = "you@example.com"
tf_state_bucket = "usstocks-tfstate-dev01-<アカウントID>"
```

`terraform.tfvars` という名前は Terraform が自動で読むので、`-var-file` は不要である。`backend.hcl` ともども gitignore 済み。

### SSH をどう通すか（グローバルIPは不要）

インスタンスを管理するには port 22 へ到達する必要があるが、**そこを 0.0.0.0/0 に開けてはいけない**。開けた瞬間から総当たりログイン試行が始まる。かといって接続元を絞るには「自分の側のアドレス」が分かっていなければならず、家庭回線では変動する。

既定では**この問題を回避する**設定にしてある。

```hcl
allow_lightsail_browser_ssh = true   # 既定
ssh_allowed_cidrs           = []     # 自分のIPは指定しない
```

`allow_lightsail_browser_ssh` は、Lightsail コンソールの「SSH を使用して接続」ボタン（ブラウザ内ターミナル）だけを通す設定である。AWS が用意した `lightsail-connect` という CIDR エイリアスを許可するもので、**自分側のアドレスは一切関与しない**。回線のIPが変わっても何もしなくてよく、port 22 がインターネットに開くこともない。

代償は、管理に AWS コンソールへのサインインが必要になることである。手元のターミナルから `ssh` したい場合は、次のいずれかを併用する。

| 方法 | 向き不向き |
|---|---|
| ブラウザSSHのみ（既定） | 固定IPが無い場合の第一選択。追加作業ゼロ |
| `scripts/allow-my-ip.sh --apply` | 現在のグローバルIPを検出して `/32` で許可する。**IPが変わるたび再実行**が必要 |
| Cloudflare Tunnel 経由のSSH | 最終形。port 22 を完全に閉じられる。cloudflared 設定後に移行する |
| `ssh_allowed_cidrs = ["0.0.0.0/0"]` | **やらないこと。**総当たりの標的になる |

`ssh_allowed_cidrs = []` は「誰も通さない」の意味だが、そのまま Lightsail へ渡すと **API 側で 0.0.0.0/0 と解釈され全開放になる**。これを避けるため、内部では到達不能な `127.0.0.1/32` に読み替えている（`lightsail.tf` の `local.ssh_cidrs`）。

#### IPv6 側も明示する必要がある

Lightsail インスタンスは既定で dualstack（IPv4 + IPv6）である。ポート規則の IPv4 と IPv6 は**独立に設定される**ため、`cidrs` だけを絞って `ipv6_cidrs` を未指定にすると、**IPv4 は施錠されているのに IPv6 経由では素通し**という状態になりうる。`plan` で `ipv6_cidrs = (known after apply)` と出ていたのがその兆候だった。

そのため両方を明示している。IPv6 を許可したい場合は `ssh_allowed_ipv6_cidrs` を使う（既定は空＝`::1/128` に読み替え）。

`terraform output open_ports` と `ssh_access_summary` で、apply 後に実際の状態を確認すること。

```bash
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

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
  --user-name "$(terraform output -raw backup_uploader_user_name)"
```

出力された鍵をインスタンスの `/etc/usstocks/usstocks.env` に書く。権限は `daily/` への `s3:PutObject` のみで、読み取りも削除もできない。鍵が漏れても蓄積した履歴は読み出せない。

ローテーション（四半期ごと、仕様書4.5）:

```bash
aws iam create-access-key --user-name <user>   # 新しい鍵を作る
# インスタンスの env を更新し、バックアップを1回走らせて成功を確認してから
aws iam delete-access-key --user-name <user> --access-key-id <古い鍵>
```

## 5. インスタンスの初期設定

`user_data` はホストの準備までしか行わない。**アプリのコードも秘密情報も user_data には入れていない** — user_data はメタデータサービスから読めるため、秘密情報を置いてはいけない（仕様書4.5）。

SSH で入って以下を行う。

```bash
# 既定の構成では Lightsail コンソールの「SSH を使用して接続」から入る。
# ターミナルから入りたい場合は先に scripts/allow-my-ip.sh --apply を実行する。
ssh ubuntu@$(terraform output -raw instance_public_ip)

# 1. 実際の値を書く
sudo vi /etc/usstocks/usstocks.env

# 2. リポジトリ読み取り専用のデプロイキーを置く
sudo -u usstocks install -d -m 0700 /var/lib/usstocks/.ssh
sudo -u usstocks ssh-keygen -t ed25519 -N '' -f /var/lib/usstocks/.ssh/id_ed25519
sudo cat /var/lib/usstocks/.ssh/id_ed25519.pub
#    → GitHub の Settings > Deploy keys に「Read only」で登録
# GitHub のホスト鍵を「検証してから」登録する。
# ssh-keyscan の出力をそのまま known_hosts へ流し込むのは、経路上の相手を
# 無検証で信頼することと同じで、中間者攻撃を検出できない。
# api.github.com は TLS で認証された経路なので、そこから取得した鍵を使う。
curl -sS https://api.github.com/meta \
  | jq -r '.ssh_keys[] | "github.com \(.)"' \
  | sudo -u usstocks tee -a /var/lib/usstocks/.ssh/known_hosts

# 登録された内容を目視確認する
sudo -u usstocks ssh-keygen -lf /var/lib/usstocks/.ssh/known_hosts

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
terraform plan

# インスタンスのプランを 2GB へ変更（仕様書12「1GBメモリ不足」への対応）
#   注意: bundle_id の変更は Lightsail では再作成になる。事前にバックアップを
#   取得し、リストア手順（docs/operations.md）を確認すること
terraform plan -var='lightsail_bundle_id=medium_3_0'

# 現在のグローバルIPからのSSHを一時的に許可する（IP変動のたびに再実行）
../../scripts/allow-my-ip.sh --apply

# ターミナルSSHをやめ、ブラウザSSHだけに戻す
terraform apply -var='ssh_allowed_cidrs=[]'
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

`terraform plan` はアカウントへの API 呼び出しを伴うため未実施である。したがって、実際に apply する前に必ず `terraform plan` の出力を目視すること。特に次の3点は plan / apply でしか判明しない。

- `lightsail_availability_zone` が `aws_region` 内に実在するか
- `create_github_oidc_provider = true` が既存プロバイダと衝突しないか
- `cidr_list_aliases = ["lightsail-connect"]` を AWS が受理するか（エイリアス名は apply 時に検証される）

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
