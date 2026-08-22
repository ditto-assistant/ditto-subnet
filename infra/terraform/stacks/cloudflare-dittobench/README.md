# dittobench.ai Cloudflare stack

This independent state root owns the stable Cloudflare edge attachment for
public subnet services. It currently attaches `backroom.dittobench.ai` to the
`ditto-subnet-backroom` Worker.

Deploy the Worker service without a production route first, then plan/apply
this stack. This prevents Wrangler and Terraform from competing for the same
custom-domain resource. Google OAuth remains the application authorization
boundary; the callback is `https://backroom.dittobench.ai/auth/callback`.

Required protected-environment configuration:

- variable `CLOUDFLARE_ACCOUNT_ID`
- variable `CLOUDFLARE_DITTOBENCH_ZONE_ID`
- secret `CLOUDFLARE_API_TOKEN`

The Terraform token needs Workers Scripts Write, Workers KV Storage Write, and
Pages Write for this account. The separate `preview` GitHub environment should
use a narrower Pages-only token for dashboard upload and deletion.

The Cloudflare token is supplied only at plan/apply time and is never stored in
the repository.

This stack also owns the Backroom MCP OAuth KV namespace. Its
`backroom_oauth_kv_namespace_id` output is consumed by the Worker deploy as the
`prod` environment variable `BACKROOM_OAUTH_KV_ID`, never by committing the id:
the namespace holds live operator grants and tokens, and this repository is
public. Recreating the namespace revokes every operator's MCP connection.

The stack also creates the `ditto-subnet-dashboard-preview` Direct Upload Pages
project. The same-run `Publish dashboard preview` reusable workflow uploads
exact-SHA dashboard bundles there after checking out the default branch; the
project has no production secrets or Backroom bindings.
