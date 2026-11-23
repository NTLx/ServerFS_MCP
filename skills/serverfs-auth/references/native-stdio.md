# Local native stdio

Local stdio has no HTTP listener, HTTP bearer or OAuth admission. The client launches the installed Core in its own local process context; filesystem policy still applies.

Use the matching native runbook and `serverfs serve --config <absolute TOML>` with trusted installed runtime. Verify MCP initialize/tools/list, intended read and expected policy denial through that stdio client. Do not register persistent jobs or add HTTP secrets for this purpose.

Remote access is a separate persistent authenticated HTTP Core deployment with optional ingress; use install and the relevant auth/Connector reference. Do not mutate stdio config into a pretend HTTP admission boundary.
