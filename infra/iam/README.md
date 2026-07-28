# Operator permissions

`terraform-operator-policy.json` is what the **human** who runs `terraform apply`
needs. It is not applied by Terraform: it has to exist before Terraform can run,
so it is attached by hand, once.

## Why this file exists separately from the GitHub deploy role

`github_oidc.tf` grants an equivalent set to the role GitHub Actions assumes, so
after the first apply the CI pipeline can manage everything. But the first apply
cannot run in CI -- the role it would assume is created *by* that apply. Someone
has to run it with their own credentials, and those credentials need these
permissions.

This was missed on the first attempt: 18 of 21 resources were created and the
run failed on `lightsail:CreateInstances`, `lightsail:AllocateStaticIp` and
`budgets:ModifyBudget`, because the operator's IAM user had neither Lightsail
nor Budgets access.

## Attaching it

Create it as a **managed** policy and attach it. An inline user policy will not
work: IAM caps those at 2048 bytes and this document is about 2700. Managed
policies allow 6144.

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ME=$(aws sts get-caller-identity --query Arn --output text | sed 's|.*/||')

aws iam create-policy \
  --policy-name usstocks-terraform-operator \
  --policy-document file://infra/iam/terraform-operator-policy.json

aws iam attach-user-policy \
  --user-name "$ME" \
  --policy-arn "arn:aws:iam::${ACCOUNT}:policy/usstocks-terraform-operator"
```

Then re-run `terraform apply`. Resources already created are in state and are
not touched again; only the missing ones are created.

### Updating it later

Managed policies are versioned, so edits replace the default version rather than
the policy:

```bash
aws iam create-policy-version \
  --policy-arn "arn:aws:iam::${ACCOUNT}:policy/usstocks-terraform-operator" \
  --policy-document file://infra/iam/terraform-operator-policy.json \
  --set-as-default
```

A policy keeps at most five versions; delete an old one with
`aws iam delete-policy-version` if that limit is reached.

## Scope

Actions are limited to this project's own names (`usstocks-*`) wherever the
service supports resource-level permissions. Lightsail and Budgets do not offer
useful resource-level scoping for the create calls, so those two statements use
`"Resource": "*"`.

Detaching it once the infrastructure is stable is reasonable -- CI holds its own
role and does not depend on this policy:

```bash
aws iam detach-user-policy --user-name "$ME" \
  --policy-arn "arn:aws:iam::${ACCOUNT}:policy/usstocks-terraform-operator"
```
