# Third-party notices

## OpenJarvis

Parts of JARVIS are ported from, or modelled on, **OpenJarvis** — "Personal AI,
On Personal Devices" — developed at Hazy Research and the Scaling Intelligence
Lab, Stanford University.

- Source: https://github.com/open-jarvis/OpenJarvis (read at commit `3b1aa17`)
- License: Apache License 2.0 — full text in
  [`third_party/OpenJarvis-LICENSE.txt`](third_party/OpenJarvis-LICENSE.txt)
- Citation: Saad-Falcon et al., *OpenJarvis: Personal AI, On Personal Devices*,
  arXiv:2605.17172 (2026)

Each JARVIS file that draws on OpenJarvis says so in its own header, naming the
OpenJarvis file it came from and what was changed. The code was rewritten for
JARVIS (Python 3.9, asyncio, JARVIS's own tool registry and storage); none of it
is a verbatim copy except where noted.

| JARVIS file | From OpenJarvis | What was taken |
|---|---|---|
| `backend/core/screening.py` | `security/injection_scanner.py`, `security/credential_stripper.py` | Injection patterns and credential formats (extended); a warning wrapper instead of a block |
| `backend/ai/telemetry.py`, `backend/systemmon/energy.py` | `telemetry/energy_nvidia.py`, `telemetry/efficiency.py`, the chat "X-ray" footer | Hardware energy counter with polling fallback; per-reply measurement |
| `backend/ai/learning.py` | `memory/extractor.py` | The fact-extraction prompt and its "any failure means no facts" design |
| `backend/ai/compression.py` | `sessions/compression.py` | Consolidating the oldest turns of a conversation (here, summarised by the model) |
| `backend/ai/prompt.py` (time line) | `prompt/time_context.py` | Telling the model to convert tool times to local time |
| `backend/skills/library.py`, `backend/skills/bundled/*` | `skills/parser.py`, `skills/loader.py`, `skills/security.py`, `skills/data/*.toml` | agentskills.io validation rules, SKILL.md parsing, trust tiers; bundled skills rewritten as SKILL.md for JARVIS's tools |
| `backend/integrations/mcp_client.py`, `backend/integrations/mcp_manager.py` | `mcp/protocol.py`, `mcp/client.py`, `mcp/transport.py`, `mcp/loader.py` | MCP handshake, response matching by id, stderr draining, HTTP session header, per-server isolation (rewritten for asyncio) |
| `backend/documents/chunker.py`, `backend/documents/index.py` | `connectors/chunker.py`, the connector → store → retrieval pipeline | Boundary order, double cap and overlap when cutting documents |
| `backend/integrations/email_account.py` | `connectors/gmail_imap.py` | Reaching a mailbox over IMAP with an app password instead of an OAuth app; TLS by default; header decoding that survives raw 8-bit bytes |
| `backend/ai/providers/openai_compat_provider.py`, `backend/ai/engines.py` | `engine/_openai_compat.py`, `engine/_discovery.py`, `intelligence/model_catalog.py` | One OpenAI-compatible client for local servers; probing running engines; model-to-hardware fit |
| `frontend/.../Conversation.tsx` (stats line, tool cards), `Panels.tsx` (efficiency) | `frontend/src/components/Chat/XRayFooter.tsx`, `ToolCallCard.tsx`, `Dashboard/EnergyDashboard.tsx`, `CostComparison.tsx` | The ideas, re-implemented in JARVIS's interface |
