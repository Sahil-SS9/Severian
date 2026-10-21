# Severian memory plugin (for Hermes Agent)

Drop-in memory provider for Hermes. Install:

```bash
git clone https://github.com/Sahil-SS9/Severian.git
cd Severian && uv sync
uv run scripts/build_hermes_plugin.py "$HERMES_HOME/plugins/severian"
hermes pm repair            # installs the pinned wheel into Hermes' managed environment
```

Then set `memory: provider: severian` in `config.yaml` and restart Hermes.

See the repo root [README](../../README.md) for features, benchmark results and configuration.

## Disclosures

- **Memory is local.** Everything is stored on the machine: by default under the Hermes data root
  (`<HERMES_HOME>/severian/`, SQLite), or in the PostgreSQL database you point `SEVERIAN_DSN` at. The engine makes no
  third-party calls.
- **Recall injects context.** The provider's purpose is returning saved memories and assembled context blocks into
  agent turns. Nothing is sent anywhere beyond what the bullets below name.
- **Optional network paths, each one you switch on:**
  - `SEVERIAN_EMBEDDING=qwen`: downloads the Qwen3-Embedding-0.6B weights from huggingface.co on first use.
  - `SEVERIAN_EMBEDDING=remote-qwen`: talks to your own sidecar socket (`SEVERIAN_EMBED_SOCKET`), no internet.
  - `SEVERIAN_USER_FACTS=1`: sends conversation turns to the model backend Hermes is already configured with, in the
    background worker you run, to short facts.
  - `SEVERIAN_RERANK`: loads a cross-encoder locally (weights download from huggingface.co on first use).
  - `severian serve`: starts the HTTP API yourself; not started by the plugin.
- **Background worker is yours.** The plugin never spawns hidden processes; the derivation worker runs when you or
  your cron run `severian work`.
- **No telemetry, no self-updating, no credential files.** Configuration is the `SEVERIAN_*` environment variables you
  set; secrets such as `SEVERIAN_PG_DSN` come from your environment, never from config files the plugin reads.