# Operator permissions

`terraform-operator-policy.json` is what the **human** who runs `terraform apply`
needs. It is not applied by Terraform: it has to exist before Terraform can run,
so it is attached by hand, once.

## Why this file exists separately from the GitHub deploy role

`github_oidc.tf` grants an equivalent set to the role GitHub Actions assumes, so
after the first apply the CI pipeline can manage everything. But the first apply
cannot run in CI — the role it would assume is created *by* that apply. Someone
has to run it with their own credentials, and those credentials need these
permissions.

This was missed on the first attempt: 18 of 21 resources were created and the
run failed on `lightsail:CreateInstances`, `lightsail:AllocateStaticIp` and
`budgets:ModifyBudget`, because the operator's IAM user had neither Lightsail
nor Budgets access.

## Attaching it

```bash
aws iam put-user-policy \
  --user-name "$(aws sts get-caller-identity --query Arn --output text | sed 's|.*/||')" \
  --policy-name usstocks-terraform-operator \
  --policy-document file://infra/iam/terraform-operator-policy.json
```

Then re-run `terraform apply`. Resources already created are in state and are
not touched again; only the failed ones are retried.

## Scope

Actions are limited to this project's own names (`usstocks-*`) wherever the
service supports resource-level permissions. Lightsail and Budgets do not offer
useful resource-level scoping for the create calls, so those two statements use
`"Resource": "*"`.

Detaching it after the infrastructure is stable is reasonable — CI holds its own
role and does not depend on this policy:

```bash
aws iam delete-user-policy --user-name <user> --policy-name usstocks-terraform-operator
```
