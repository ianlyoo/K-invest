# OWNER_ACTIONS — K-invest

GitHub API cannot upload repository social previews. Manual steps required.

## Social preview — manual upload

1. Go to `https://github.com/ianlyoo/K-invest/settings` → `General` → `Social preview` → `Edit` → `Upload an image`.
2. Upload file `docs/assets/social-preview.png` (1280×640, <1MiB, deterministic sharp PNG with compressionLevel 9).
3. Save.

**Verification queries:**

```bash
# Pages OG (automatic after push) — should return 200 and correct og:image
curl -fsSL https://ianlyoo.github.io/K-invest/ | grep -o 'og:image[^>]*content="[^"]*"'
curl -fsSL https://ianlyoo.github.io/K-invest/assets/social-preview.png -o /tmp/p.png && ls -l /tmp/p.png

# GitHub repo social preview (after manual upload) — default is opengraph.githubassets.com
gh api graphql -f query='query{repository(owner:"ianlyoo",name:"K-invest"){openGraphImageUrl}}'

# Topics / homepage verbatim
gh repo view ianlyoo/K-invest --json description,homepageUrl
gh api repos/ianlyoo/K-invest/topics --jq '.names | sort'
```

**Determinism check (re-build should yield identical SHA):**

```bash
# Rebuild deterministically (compressionLevel 9, no timestamp)
node docs/assets/gen.js  # or bun run social-preview:build
sha256sum docs/assets/social-preview.png
# compare with committed SHA
```

**Note:** Do not use API for social preview upload — GitHub write API does not exist for this asset. Record as OWNER_ACTION_REQUIRED.
