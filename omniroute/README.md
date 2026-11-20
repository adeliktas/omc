# OmniRoute for OMC

OMC runs [OmniRoute](https://github.com/diegosouzapw/OmniRoute) as its model gateway. You add your
LLM servers and accounts in OmniRoute's dashboard, and Mission Control routes work through it.

The image is built from the upstream release pinned in `UPSTREAM`, plus `patches/omc.patch`:

- **Prompt-aware routing:** routing that reads the prompt, with round-robin within the same pool.
- **Connection policy during failover:** an API key's connection policy is kept when failover
  moves to an inner account.
- **Private lane:** an optional, self-authenticated route `/api/v1/private/chat/completions` for
  boards whose content may only reach one local model.
  - It is enabled only when `OMC_PRIVATE_LANE_MODEL` and `OMC_PRIVATE_LANE_BASE_URL` (an
    OpenAI-compatible `/v1` base URL) are set.
  - The model must be marked `fully_trusted` on a single `vllm` provider node.
  - When unset, every request to the lane is refused.
- **Streaming slots and token refresh:** round-robin holds a target's concurrency slot until a
  stream ends; a token refresh's background work queues for the refresh lane again once it ends;
  a Codex import stores the new access token's expiry.

Settings that replace older local patches (upstream covers them since v3.8.54):

- Qwen behind vLLM wants the system message first: `OMNIROUTE_STRICT_SYSTEM_PROVIDERS=<node id>`.
- Qwen thinking off by default: a reasoning rule for the qwen models with target effort `none`.

Build:

```bash
omniroute/build.sh omniroute:omc-local --target runner-base
```

`deploy/install.sh` runs this for each instance.
