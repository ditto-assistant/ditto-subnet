# GCP Platform state single-writer cutover

The public `infra/terraform/stacks/gcp-platform` root deliberately uses the
existing `gs://ditto-app-dev-tfstate/gcp-platform` state object. No state copy or
`terraform state mv` is required. The safety boundary is transferring ownership
of that one state from the private infra root to this root without a dual-writer
window.

## Cutover

1. Merge the complete monorepo stack and record its exact `main` SHA.
2. Disable every plan/apply workflow and scheduled Terraform job for the private
   `ditto-assistant/infra` GCP Platform root. Preserve the repository and its
   history for rollback; do not delete the state bucket or object.
3. Confirm no private-root Terraform job is running and no human has an
   outstanding local apply. Record the workflow-disable audit link in the
   monorepo cutover issue.
4. Enable the protected `infra-plan` environment in `ditto-subnet` and run an
   exact-main `gcp-platform` plan. Review the complete plan, including removal
   of legacy service-repository WIF principals. Do not apply if it proposes
   replacement or deletion caused only by an address change between roots;
   reconcile resource addresses first with reviewed `moved` blocks or explicit
   state moves.
5. Apply only the sealed plan through the protected `infra-apply` environment.
   Verify the state serial advanced once and run a second plan; it must be empty.

## Rollback

Disable the public plan/apply workflow before re-enabling the private root. Only
one root may plan or apply against the shared prefix at a time. If the public
root has already changed resource addresses or state, update the private root
to that exact state schema before any rollback apply.
