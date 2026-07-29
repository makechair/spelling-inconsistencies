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

## 1.5 `sig-games.com` の調査結果（2026-07-29 実測）

上記の調査を実施した結果、ゾーンの全レコードは5行だった。

```
sig-games.com.                                    A      ALIAS -> d3job3mbxfy7bw.cloudfront.net.
sig-games.com.                                    NS     ns-307.awsdns-38.com. 他3件
sig-games.com.                                    SOA    ns-307.awsdns-38.com. ...
_de2cfe4f1f83eacb4a39d3480f100a3a.sig-games.com.  CNAME  _4abb4944ca72b1ccf3d8e5fd2d33ece7.zfyfvmchrl.acm-validations.aws.
_855ab008b7e63654e2a90d0948dcf9ae.www.sig-games.com. CNAME _210f7f32d989966f6e3218bcb4c0ff38.zfyfvmchrl.acm-validations.aws.
```

判定:

- **DNSSEC は `NOT_SIGNING`。** 1.3 の対応は不要、待ち時間もゼロ。
- **MX / TXT が存在しない。** このドメインでメールは運用していないので、
  SPF・DKIM・DMARC の移行漏れリスクはない。
- ALIAS は apex の1行のみ。
- ブログの実体は CloudFront ディストリビューション `d3job3mbxfy7bw.cloudfront.net`。

### Cloudflare 側で作成するレコード（これで全部）

| # | Name（Cloudflare の入力欄） | Type | Content | Proxy |
|---|---|---|---|---|
| 1 | `sig-games.com` | **CNAME** | `d3job3mbxfy7bw.cloudfront.net` | **DNS only（灰）** |
| 2 | `_de2cfe4f1f83eacb4a39d3480f100a3a` | CNAME | `_4abb4944ca72b1ccf3d8e5fd2d33ece7.zfyfvmchrl.acm-validations.aws` | DNS only |
| 3 | `_855ab008b7e63654e2a90d0948dcf9ae.www` | CNAME | `_210f7f32d989966f6e3218bcb4c0ff38.zfyfvmchrl.acm-validations.aws` | DNS only |

`NS` と `SOA` はコピーしない（Cloudflare が自ら管理する）。
`stocks` は Tunnel が自動作成するので、ここでは作らない。

**#1 は A ではなく CNAME である。** A + IP で作ると、CloudFront のアドレスが
変わった日にブログが落ちる。1.2 で ALIAS を洗い出したのはこれを防ぐためである。

**#2 と #3 は Cloudflare の自動スキャンでは発見されない。** ACM の検証用レコードは
ランダムな16進を名前に持ち、スキャンは「よくあるレコード名」しか試さない。
必ず手で作ること。

### 実際のスキャン結果（2026-07-29）

このゾーンで Add a site を実行した結果、Cloudflare が提示したのは以下だった。

```
A  sig-games.com  3.175.34.33   Proxied
A  sig-games.com  3.175.34.56   Proxied
A  sig-games.com  3.175.34.20   Proxied
A  sig-games.com  3.175.34.102  Proxied
```

**4件すべて誤りである。**

- ALIAS を DNS 越しに見た結果の A レコードで、その瞬間の CloudFront のアドレスを
  焼き付けている。同じ時刻に別の場所から引くと `18.238.136.48` が返っており、
  **同一名が場所と時刻で違うアドレスを返す**のが CloudFront の正常な挙動である。
  固定すれば壊れるのは時間の問題にすぎない。
- 既定で Proxied（オレンジ）になっている。ブログの経路に Cloudflare が割り込む。
- ACM 検証用 CNAME 2件は、予告どおり1件も発見されていない。

したがって手順は「スキャン結果を承認する」ではなく、**4件を削除し、上の表の3件を
手で作る**である。

画面に出る2つの案内も、どちらも従わない。

- 「MX を追加せよ」 → このドメインでメールは運用していない（1.5 のとおり
  Route 53 側にも MX がない）。
- 「www の A/AAAA/CNAME が必要」 → `www` は移行前から解決しない。ここで作ると
  移行前後で挙動が変わり、問題が出たときの切り分けができなくなる。

完了時点でレコードは3件、**すべて灰色**。オレンジがゼロの状態が正しい
（`stocks` は §4 で Tunnel が自動作成し、それだけがオレンジになる）。

そして厄介なのは、**落としてもその場では何も壊れない**ことである。証明書は発行済み
なのでブログは見え続ける。壊れるのは ACM が自動更新をかける時 — 最大13ヶ月後、
移行の記憶が完全に消えた頃に、原因不明で HTTPS が切れる。

### 既存の状態としてそのまま再現すること

`www.sig-games.com` は**移行前から名前解決しない**（A/CNAME が存在しない）。
ACM 証明書には含まれている（#3 の検証レコードがある）ので、CloudFront 側では
代替ドメイン名として設定済みだが DNS を作っていない状態と見られる。

移行時に「ついでに直す」ことはしない。移行後に問題が出たとき、それが移行由来か
元からかを切り分けられなくなる。直すなら移行が落ち着いてから別作業として行う。

### 切り替え前の照合（実データ版）

```bash
CF_NS=xxx.ns.cloudflare.com        # Cloudflare が割り当てたもの
R53_NS=ns-307.awsdns-38.com

# apex: Route 53 は A、Cloudflare は CNAME を返す。応答の型が違うのは想定どおり。
# 最終的に解決される先が同じ CloudFront であることを見る。
dig +short "@$R53_NS" sig-games.com A
dig +short "@$CF_NS"  sig-games.com A

# ACM 検証レコードは完全一致すること。
#
# 空を不合格として明示的に弾く。単純な [ "$a" = "$b" ] は、両方とも空のときに
# 一致と判定してしまう。名前を打ち間違えた場合も、コピペでレコード名が壊れた
# 場合も、dig は静かに空を返すので、素通しの比較では合格に見える。
# ACM 検証レコードは欠落しても13ヶ月後まで何も壊れないため、
# 偽の合格を出す照合は照合しないより悪い。
ACM1=_de2cfe4f1f83eacb4a39d3480f100a3a.sig-games.com
ACM2=_855ab008b7e63654e2a90d0948dcf9ae.www.sig-games.com

for n in "$ACM1" "$ACM2"; do
  a=$(dig +short "@$R53_NS" "$n" CNAME)
  b=$(dig +short "@$CF_NS"  "$n" CNAME)
  echo "name: $n"
  echo "  r53: ${a:-<EMPTY>}"
  echo "  cf : ${b:-<EMPTY>}"
  if [ -z "$a" ] || [ -z "$b" ]; then echo "  => NG (空。名前が違う可能性)"
  elif [ "$a" = "$b" ]; then echo "  => OK"
  else echo "  => DIFF"; fi
done
```

ACM の2行が `OK` で、かつ `_4abb4944ca72...` / `_210f7f32d989...` の実値が
両側に表示されること。apex は両方とも CloudFront のアドレスを返せば合格
（アドレスの一致は不要。順序も一致しなくてよい）。

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
