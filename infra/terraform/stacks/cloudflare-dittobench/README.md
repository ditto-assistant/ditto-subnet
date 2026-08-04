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

The Cloudflare token is supplied only at plan/apply time and is never stored in
the repository.
