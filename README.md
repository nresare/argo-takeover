# argo-takeover

A tool for migrating Kubernetes workloads from Helm to Argo CD management.
It verifies that Argo CD has adopted every object a Helm release owns, then
removes what Helm left behind so the objects look as if Argo CD created them.

## How it works

The set of objects a release owns is read from Helm's own state: the
`sh.helm.release.v1.*` secret in the release namespace, whose `release` key
contains the rendered manifest as base64(gzip(json)).

**check** fetches every object from that manifest and verifies it carries the
`argocd.argoproj.io/tracking-id` annotation, i.e. that Argo CD tracks it.

**cleanup** removes Helm's leftovers from each tracked object: every label and
annotation whose key or value contains "helm" (case-insensitively) — which
covers at least the `app.kubernetes.io/managed-by` and `helm.sh/chart` labels
and the `meta.helm.sh/*` annotations. The mechanism is a targeted JSON patch
naming exactly those keys, so nothing else on the object — its `data`, `spec`,
or any other label or annotation — is ever touched. Once every object is
clean, the Helm release secrets are deleted so a stray `helm upgrade` can no
longer touch the objects.

## Usage

Verify a takeover:

```
argo-takeover check <namespace> <release>
```

Preview the cleanup (dry run, changes nothing):

```
argo-takeover cleanup <namespace> <release>
```

Perform the cleanup and delete the Helm release secrets:

```
argo-takeover cleanup --apply <namespace> <release>
```

Cluster access goes through `kubectl`, using whatever context is currently
active.

## Exit codes

- `0` – success: everything tracked (check) or everything clean (cleanup)
- `1` – some objects are untracked, need review, or failed
- `2` – error, such as no release secret found

## Development

```
uv run pytest
uv run ruff check
uv run ruff format --check
uv run ty check
```

## License

[MIT](LICENSE)
