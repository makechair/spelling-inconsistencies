# Cloudflare Tunnel / Access の設定

既存ドメインへの「相乗り」を前提にした手順。ここでは例として、Route 53 Domains で
登録済み・ブログで使用中の `sig-games.com` に `stocks.sig-games.com` を追加する。

## 0. 結論

**相乗りできる。** 本システムはサブドメインしか使わないので、ブログと同じドメインを
共有して問題ない。

ただし無料で済む代わりに、**ゾーン全体の権威DNSを Cloudflare へ移す**必要がある。
Cloudflare Tunnel の public hostname は、そのゾーンが Cloudflare 上にあることを
前提に DNS レコードを自動作成するためである（Free/Pro では「full setup」＝
ネームサーバー委任のみ。レコード単位で委任する CNAME setup は上位プラン限定）。

つまり、**この作業でリスクを負うのは株チャートではなくブログの方**である。
ブログの DNS レコードを移行漏れしたまま NS を切り替えると、ブログが落ちる。

移行そのものは難しくないが、「Cloudflare の自動スキャンに任せて NS を切り替える」
のは危険である。スキャンは ALIAS レコードを取りこぼす（後述）。

### 相乗りしない選択肢

| 選択肢 | 費用 | ブログへの影響 |
|---|---|---|
| `sig-games.com` に相乗り | 0円 | **あり**（NS 移行を伴う） |
| 本システム専用に別ドメインを取る | 年 $10 前後 | なし |
| Cloudflare をやめて Tailscale にする | 0円 | なし（ドメイン自体が不要） |

ブログを止めたくない気持ちが強いなら、専用ドメインを1つ買うのが最も静かな解決である。
`stocks-<なにか>.com` のような使い捨てで構わない。以下は相乗りする場合の手順。

## 1. 移行前の調査

### 1.1 現在のレコードを控える

```bash
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name sig-games.com \
  --query 'HostedZones[0].Id' --output text | sed 's|/hostedzone/||')

aws route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" \
  > ~/sig-games-records.json

# 人間が読む形に
jq -r '.ResourceRecordSets[]
  | [.Name, .Type, (.TTL|tostring),
     (if .AliasTarget then "ALIAS -> " + .AliasTarget.DNSName
      else ([.ResourceRecords[].Value] | join(" ; ")) end)]
  | @tsv' ~/sig-games-records.json | column -t -s $'\t'
```

このファイルは移行後の照合とロールバックの両方に使う。消さないこと。

### 1.2 ALIAS レコードを特定する

```bash
jq -r '.ResourceRecordSets[] | select(.AliasTarget) | "\(.Name)\t\(.Type)\t\(.AliasTarget.DNSName)"' \
  ~/sig-games-records.json
```

**ここが最大の落とし穴である。** ALIAS は Route 53 独自の拡張で、外から DNS で
問い合わせると A レコード（IPアドレス）として応答が返る。Cloudflare の自動スキャンは
DNS 越しに見ているので、CloudFront/S3/ALB を指す ALIAS を「その時点の IP を持つ
A レコード」として取り込む。**取り込んだ直後は正しく見えるが、CloudFront の IP は
変わるので、いずれ壊れる。**

Cloudflare 側では ALIAS の代わりに **CNAME を使う**。Cloudflare は apex（`sig-games.com`
自体）に対しても CNAME を許容し、応答時に A へ展開する（CNAME flattening）。

| Route 53 | Cloudflare で置き換えるもの |
|---|---|
| `sig-games.com` A ALIAS → `dxxxx.cloudfront.net` | `sig-games.com` CNAME → `dxxxx.cloudfront.net` |
| `www` A ALIAS → 同上 | `www` CNAME → 同上 |

### 1.3 DNSSEC を確認する

```bash
aws route53 get-dnssec --hosted-zone-id "$ZONE_ID"
```

`Status.ServeSignature` が `SIGNING` なら、**NS 変更の前に DNSSEC を無効化する**。
有効なまま委任先を変えると、TLD に残った DS レコードと Cloudflare の応答が食い違い、
検証するリゾルバからは名前解決が完全に失敗する（NXDOMAIN ではなく SERVFAIL に
なるので、原因が分かりにくい）。無効化してから DS レコードが TLD から消えるまで
待つこと。

### 1.4 メール系レコードを確認する

MX・SPF（TXT）・DKIM（CNAME/TXT）・DMARC（TXT）が生きているなら、
移行漏れはメール不達に直結する。1.1 の一覧に含まれているので、
必ずチェックリストに入れる。

## 2. Cloudflare 側の準備（NS はまだ変えない）

1. 無料アカウントを作成する。
2. **Add a site** → `sig-games.com` → Free プラン。
3. 自動スキャン結果が出るので、**1.1 の一覧と1行ずつ突き合わせる**。
   - 足りないレコードを手で追加する。
   - ALIAS 由来の A レコードを 1.2 の CNAME に置き換える。
   - 既存レコードはすべて **DNS only（灰色の雲）** にする。
     オレンジ（proxied）にすると Cloudflare が経路に入り、ブログの挙動が変わる。
     今回の目的はブログを変えないことなので、既存はすべて灰色でよい。
   - `NS` と `SOA` は Cloudflare が自ら管理するので、コピーしない。
4. Cloudflare が割り当てたネームサーバー2つを控える（`xxx.ns.cloudflare.com`）。

### 切り替え前の照合

NS を変える前に、Cloudflare のネームサーバーへ直接問い合わせて、
Route 53 と同じ答えが返ることを確認する。委任前でも応答はする。

```bash
CF_NS=xxx.ns.cloudflare.com     # 3. で控えたもの
R53_NS=$(aws route53 get-hosted-zone --id "$ZONE_ID" \
  --query 'DelegationSet.NameServers[0]' --output text)

for name in sig-games.com www.sig-games.com; do
  for type in A AAAA CNAME MX TXT; do
    a=$(dig +short "@$R53_NS" "$name" "$type" | sort | tr '\n' ' ')
    b=$(dig +short "@$CF_NS"  "$name" "$type" | sort | tr '\n' ' ')
    [ "$a" = "$b" ] || echo "DIFF $name $type: r53=[$a] cf=[$b]"
  done
done
```

ALIAS を CNAME に置き換えた行は当然差分として出る（Route 53 は A を返し、
Cloudflare は CNAME を返す）。それ以外に差分が出ないことを確認する。
差分が「なにもない」ことではなく、**差分が説明できること**が合格条件である。

## 3. ネームサーバーの切り替え

Route 53 Domains（Route 53 コンソール → Registered domains → `sig-games.com`）で、
ネームサーバーを Cloudflare のものに変更する。ドメインの管理は AWS のままでよい。
Cloudflare へのドメイン移管は不要である。

```bash
aws route53domains get-domain-detail --region us-east-1 --domain-name sig-games.com \
  --query 'Nameservers[].Name'
```

- Cloudflare 側でゾーンが Active になるまで数分〜数時間。
- `.com` の委任 NS はキャッシュが最大2日ある。その間は Route 53 と Cloudflare の
  両方が応答するので、**Route 53 のホストゾーンは消さないこと**。内容も変えない。
  少なくとも3日は残し、ブログが正常なことを確認してから削除する
  （ホストゾーンは月 $0.50 かかるが、保険としては安い）。

### ロールバック

ブログが壊れたら、Route 53 Domains のネームサーバーを元の4つ（1.1 の調査時点で
`get-hosted-zone` が返していたもの）に戻す。ホストゾーンを消していなければ、
それだけで元に戻る。これがホストゾーンを残す理由である。

## 4. Tunnel の作成

1. Zero Trust ダッシュボード → Networks → Tunnels → Create a tunnel → Cloudflared。
2. トークンをコピーする。→ インスタンスの `/etc/usstocks/usstocks.env` の
   `CLOUDFLARE_TUNNEL_TOKEN`。
3. Public hostname:
   - Subdomain: `stocks`、Domain: `sig-games.com`
   - Service: `HTTP` → `api:8000`（Docker Compose 構成）
     もしくは `http://127.0.0.1:8000`（systemd 構成）

`stocks.sig-games.com` の DNS レコードは Cloudflare が自動で作る
（`<tunnel-id>.cfargotunnel.com` への proxied CNAME）。手で作る必要はない。
**このレコードだけはオレンジ（proxied）である** — Tunnel も Access も
Cloudflare が経路上にいることが前提なので、灰色にはできない。

Universal SSL（無料）は apex と1階層のサブドメインを自動でカバーするので、
`stocks.sig-games.com` の証明書は自動で発行される。

## 5. Access アプリケーション

1. Zero Trust → Access → Applications → Add → Self-hosted。
2. Application domain: `stocks.sig-games.com`
3. Identity provider に Google を設定する。
4. Policy: **Allow — Emails — 自分のアドレスだけ**。
   既定拒否のままにし、Bypass ポリシーは作らない（仕様書4.5）。
5. **Application Audience (AUD) Tag をコピーする。**
   → `/etc/usstocks/usstocks.env` の `USSTOCKS_CF_ACCESS_AUD`。
6. チームドメイン（`yourteam.cloudflareaccess.com`）
   → `USSTOCKS_CF_ACCESS_TEAM_DOMAIN`。

Access はゾーン単位ではなくアプリケーション単位で効くので、
**ブログには一切影響しない**。`sig-games.com` と `www.sig-games.com` は
Access のポリシー対象外のまま、これまで通り誰でも見られる。

> Access の設定だけに依存しない。アプリ側でも JWT を検証しているので、
> ポリシーを消しても即座に公開状態にはならない
> （[spec-review A-4](spec-review.md)）。

## 6. 確認

```bash
# 認証前は到達できない（仕様書3.5）
curl -sI https://stocks.sig-games.com | head -1        # -> 302（Access へ）

# ブログが無事であること
curl -sI https://sig-games.com | head -1
dig +short sig-games.com
```

別の Google アカウントで `stocks.sig-games.com` を開き、拒否されることも確認する。

## 7. チェックリスト

- [ ] `~/sig-games-records.json` を取得した
- [ ] ALIAS レコードを列挙し、Cloudflare 側で CNAME に置き換えた
- [ ] DNSSEC が無効であることを確認した
- [ ] MX / SPF / DKIM / DMARC を移した
- [ ] 既存レコードはすべて DNS only（灰色）にした
- [ ] Cloudflare の NS へ直接問い合わせ、説明できない差分がないことを確認した
- [ ] Route 53 Domains でネームサーバーを変更した
- [ ] Route 53 ホストゾーンを**消さずに**残した
- [ ] ブログが正常に見えることを確認した
- [ ] Tunnel を作り、トークンを env に書いた
- [ ] Access アプリを作り、AUD とチームドメインを env に書いた
