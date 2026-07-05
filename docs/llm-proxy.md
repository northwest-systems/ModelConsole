# LLM Proxy Contract

ModelConsole の adapter 変換 proxy は、個別 provider の CLI option ではなく、provider stdout / SDK message を ModelConsole の内部 stream に変換する境界である。

## Base Contract

基準形は Claude Code の print / Agent SDK stream に合わせる。

- top-level message: `user`, `assistant`, `system`, `result`
- assistant content block: `text`, `tool_use`, `tool_result`
- partial stream event: `content_block_delta` with `text_delta`

ModelConsole server は provider から受け取った JSON をそのまま client API に出さない。必ず `mcon.adapters.llm_proxy.convert_provider_event()` を通し、以下の internal event に変換する。

| Event | Purpose |
| --- | --- |
| `llm_event` | Raw provider message retention. Debug / audit 用で、UI 表示の主経路ではない。 |
| `assistant_delta` | UI が表示する assistant text delta。 |
| `llm_tool_call` | Claude Code `tool_use` content block の正規化。 |
| `llm_tool_result` | Claude Code `tool_result` content block の正規化。 |
| `llm_result` | Claude Code `result` message の正規化。 |
| `llm_system` | Claude Code `system` message の正規化。 |
| `llm_user` | Claude Code `user` message の正規化。 |
| `llm_stream_event` | partial stream event の raw retention。 |

## Provider Compatibility

現在の provider 実行は Codex CLI だが、client / TUI は `codex_event` に依存しない。Codex 固有 event は adapter で `llm_event` と `assistant_delta` へ変換する。

古い server との互換のため、TUI は legacy `codex_event` の text extraction を残す。ただし新しい server が出す主系は `assistant_delta` である。

## Security Boundary

LLM proxy は tool 実行を許可しない。tool request を表す provider message を `llm_tool_call` として表現するだけで、実行は orchestration / MCP / executor の policy 判定を通る。

provider CLI の permission flag や output option は実装詳細であり、この contract の代替ではない。client API と TUI は provider-specific option ではなく、ここで定義した internal event のみを表示・制御に使う。
