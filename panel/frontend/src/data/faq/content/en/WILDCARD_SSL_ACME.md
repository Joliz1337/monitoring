# Issuing via DNS validation

A wildcard certificate can't be obtained with the usual HTTP check: Let's Encrypt requires proof of domain ownership through DNS. The panel does that automatically via the Cloudflare API.

## What happens during issuance

1. The panel requests a certificate for `*.domain` and receives a validation value from Let's Encrypt.
2. A temporary `_acme-challenge` TXT record is created in your zone through the Cloudflare API.
3. Let's Encrypt verifies the record, the panel receives the certificate, and the temporary record is removed.

## Cloudflare token requirements

The token needs permission to read the zone and edit its DNS records. Scope it to that single zone — full account access is unnecessary.

## Common failures

| Symptom | Cause |
|---|---|
| Authorisation error | The token is wrong, revoked or lacks DNS edit rights |
| Zone not found | The domain isn't served by Cloudflare, or it's mistyped |
| Validation failed | The DNS record hasn't propagated — retrying in a few minutes usually helps |
| Rate limit reached | Let's Encrypt caps issuance per domain: wait, and don't reissue without reason |

## Good to know

- Renewal uses the same method and needs no involvement from you; the panel starts renewing well before the last day.
- Certificates live 90 days — a Let's Encrypt rule that can't be extended.
- Repeated issuance quickly hits the rate limits, so test your configuration with the certificate you already have.
