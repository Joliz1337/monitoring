# HAProxy certificates

TLS certificates on one node: issue via Let's Encrypt, upload your own, renew.

## Issuing via Let's Encrypt

1. The domain must point at this server with an A or AAAA record.
2. Port 80 must be open — domain ownership is verified over it.
3. Provide the domain and an email, start the issue — the panel runs the challenge itself.
4. Reference the certificate domain in the HAProxy rule and reload the config.

Your own certificate is uploaded as a file: a `.pem` containing the chain and private key works, or two separate files that the panel will merge.

## Good to know

- Let's Encrypt certificates last 90 days; the "expiring soon" mark appears 30 days out.
- Details show who it was issued to and by, all domains it covers and the expiry date.
- A wildcard certificate can't be issued this way: `*.example.com` requires DNS validation — that's the separate Wildcard SSL page, which also distributes the certificate across all nodes.

## When issuing fails

| Cause | How to check |
|---|---|
| Domain doesn't point at the server | `dig +short domain` — the address must match the node's IP |
| Port 80 closed or busy | `ss -tlnp \| grep :80` on the node, plus firewall rules |
| Let's Encrypt rate limit hit | Five failed attempts per hour per domain — wait it out |
| Certificate issued but not in effect | It must be referenced in a rule, then the config reloaded |
