# Scripts

| File | Purpose |
|---|---|
| `entrypoint.sh` | Optional container entrypoint that waits for dependencies and dispatches by role (`api` / `worker` / `beat`). The Compose stack does not use it — it runs migrations as a separate one-shot service, which is the safer pattern. |

Day-to-day operator tooling lives in the `smarttender-admin` CLI
(`app/cli.py`), not here:

```bash
smarttender-admin connectors          # which sources are runnable, and why not
smarttender-admin dry-run tuneps      # run a connector, write nothing
smarttender-admin score <tender-id>   # explain a score
smarttender-admin seed                # create the default schedules
smarttender-admin health              # probe every dependency
```
