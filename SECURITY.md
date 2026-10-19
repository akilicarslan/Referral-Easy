# Security and public source

ReferralEasy is a local demo with synthetic records. It has no public user login.

## Run the demo locally

Compose binds every published port to `127.0.0.1`. The gateway also binds to loopback
when run directly, unless `HOST` is set. Containers set `HOST=0.0.0.0` internally;
Compose restricts their published ports on the host.

The demo profile provides patient and admin session endpoints without login. OpenSearch
has its security plugin disabled, and the local database uses demo credentials. Keep
these services on the local machine. Do not expose them through port forwarding,
tunnels, or a public reverse proxy.

For public hosting, configure an identity provider, service authentication, restricted
ingress, TLS, protected storage and secret management. Use a non-demo `APP_ENV` so the
fixture session and seeding routes are absent. Setting `APP_ENV=production` alone does
not supply a production identity provider or harden the rest of the demo stack.

## Protect local configuration

Keep credentials in the ignored `.env` or an external secret manager. `.env.example`
contains placeholders. Do not add local configuration, private keys, assistant working
memory, browser logs, database files, or installed dependencies to public source.

Docker build contexts exclude local secrets, Git metadata, caches and dependencies.
Each Dockerfile also has a default-deny ignore file that admits its required COPY inputs.
If a Dockerfile changes, update its ignore file and verify every COPY input is available.

## Check a release

Run these checks before publication:

```sh
python3 scripts/check_public_release.py
gitleaks git . --config .gitleaks.toml --redact
```

CI and pre-commit also run the privacy guard. Gitleaks detects supported credential
patterns. The privacy guard rejects private filenames and machine-specific home or
temporary paths; it reports locations without printing file contents.

The public baseline uses a neutral contributor identity. Keep future Git author and
committer names and emails suitable for publication. File scans cannot remove personal
identity from existing commit metadata.

For a source archive, use `git archive HEAD`. A whole-folder archive can include ignored
secrets and local data. If a real credential is published, revoke or rotate it at its
provider; removing a file or replacing history does not invalidate that credential.

These checks reduce accidental exposure. They do not replace dependency vulnerability
checks, runtime security testing, or a deployment review.
