# vendor/

Third-party API specifications, kept here so driver work does not depend on a
vendor's docs site being reachable.

## `fornex-openapi.json`

Fornex Public API, OpenAPI 3.0.3. Refresh with:

```bash
curl -s -A 'curl/8' 'https://fornex.com/api/schema?format=json' -o vendor/fornex-openapi.json
```

That endpoint returns 200 to plain `curl` while much of `fornex.com` returns 403
to browsers and to fetchers behind Cloudflare — do not scrape the HTML docs page,
which was the original method and is unnecessary.

**One modification from upstream:** the example Slack webhook URL in the
`tickets/webhook_set` documentation has been replaced with
`https://hooks.slack.com/services/T00000000/B00000000/EXAMPLE-REDACTED`. The
upstream value is a full-length, real-looking `hooks.slack.com` URL, and GitHub's
push protection blocks it. It is not ours and may well be live, so it is redacted
rather than republished. If you refresh the file with the command above, redact it
again before committing.
