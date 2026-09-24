# Sample AWS infrastructure

Use this Terraform only in a **dedicated disposable AWS account** with public or synthetic data.
The current runner forwards its exported AWS credentials to the evaluated agent (S1). Its caller
policy also spans MicroVM resources in the account and region. Read the
[credential restrictions](../../eval/README.md#aws-credentials-and-isolation-s1) before provisioning.

## Separate provisioning and execution

Use two identities:

| Identity | Purpose | Required boundary |
| --- | --- | --- |
| Provisioning identity | Create or remove the sample infrastructure and attach its policy | Use only for Terraform and account setup; keep its credentials out of agent runs |
| `eval-harvest-runner` role | Run the evaluation with short-lived credentials | Only the reviewed sample caller policy; no administrative, unrelated, or cross-account grants |

Create the runner role through your normal IAM administration process before applying Terraform.
Restrict its trust policy to the identities allowed to run this sample. This module attaches a
policy to that existing role; it does not create the runner role or configure your sign-in method.
The build and execution roles created by this module serve different purposes.

Attaching `eval-harvest-eval-caller` to a broadly privileged role does not remove that role's other
permissions. Review its complete effective access, including other policies and role trust
relationships. The supplied policy is a starting point for the sample, not an enforced upper limit.

## Configure and apply

From the repository root:

```bash
cp infrastructure/terraform/terraform.tfvars.example infrastructure/terraform/terraform.tfvars
```

Replace `SAMPLE_ACCOUNT_ID` in `eval_caller_principal_arns` with the disposable account's ID and
adjust the role name if needed. Keep the list explicitly set to the dedicated runner role. Do not
leave it empty: the existing default attaches the caller policy to whoever runs Terraform.
`terraform.tfvars` is ignored by Git; do not store credentials in it.

Set the region and permitted models deliberately. Review `iam.tf`, including artifact-bucket
access, PassRole to the two service roles, and account/region-wide MicroVM operations. This
configuration does not enforce the dedicated-account requirement or remove other role grants.

In a fresh terminal with no exported AWS access keys, select a **provisioning** profile for the
sample account and check the identity:

```bash
export AWS_PROFILE=eval-harvest-provision
aws sts get-caller-identity
```

Stop if this is not the intended sample account and provisioning identity. Then inspect the plan
before applying:

```bash
mise run infra-plan
mise run infra-apply
```

Check that the plan attaches the caller policy only to the dedicated runner role. Do not broaden
an access-denied fix to administrative permissions; identify the required action and resource.
Keep Terraform state and plan output private. Close the provisioning terminal after setup and
follow the [restricted runner session instructions](../../eval/README.md#run) in a fresh terminal.

Short-lived runner credentials remain usable by the agent until they expire or are revoked.
Sample artifact access, resource modification, and charges remain possible within the role's
permissions. Configure spending alerts and suitable service quotas before running; alerts do not
enforce a spending cap.

## Cleanup

Stop active trials and terminate remaining sample MicroVMs before cleanup. Resources created by
Harbor at runtime are not all managed by this Terraform state; inspect and remove remaining sample
images and runtime resources separately.

Using the separate provisioning identity, confirm the account again and run:

```bash
terraform -chdir=infrastructure/terraform plan -destroy
terraform -chdir=infrastructure/terraform destroy
```

The artifact bucket uses `force_destroy`; destruction deletes its contents. Retain only reviewed
outputs before cleanup. Remove the separately created runner role and its access when the sample
is no longer needed, and revoke sessions if exposure is suspected.
