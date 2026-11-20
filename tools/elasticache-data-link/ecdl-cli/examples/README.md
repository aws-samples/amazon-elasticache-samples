# Examples

## `checkout-cache/` — the current model

One file: `link.yaml`. Start here.

```bash
ecdl validate ecdl-cli/examples/checkout-cache/link.yaml
```

Everything about the source and the target is read from their ARNs, so the file
holds only what cannot be discovered: which tables, and how each cached object is
shaped.
