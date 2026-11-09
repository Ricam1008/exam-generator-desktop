# Generation time and token costs

Runtime depends on readable source length, question targets, output length, reasoning effort, retries and provider capacity. Parallelism can reduce wall time, but it does not inherently reduce the number of tokens billed.

## Before generating

Use the preparation summary to review course grouping, new versus reusable exams, question targets, nominal request counts and unreadable files. Archive folders remain separate groups unless their assignments change. Matching filenames in different formats are suggestions for review, not proof that their contents are identical.

An estimate without model measurements is labelled uncalibrated. A synthetic sample can provide a provisional range; it cannot establish performance on long or difficult real material. See [material and model planning](PREFLIGHT_PLAN.md) for the estimator's assumptions.

## Reducing unnecessary work

- Select only the source versions needed for the intended exam. Do not silently remove distinct versions.
- Reuse unchanged exams. Changing parallelism alone does not require regeneration.
- Keep stable source/blueprint prefixes ahead of changing batch instructions so providers can reuse cached input where supported.
- Serialize blueprint JSON compactly without removing fields.
- Validate structured responses before publication and retain error/cancellation handling.

The app defaults to Low thinking for DeepSeek and offers Off, Low, High and Max. Medium maps to High at the provider. A lower effort may reduce reasoning work, but equal exam quality and a specific cost saving require a representative comparison. The app does not automatically switch models or reduce requested question counts.

## Reading usage reports

Successful jobs persist provider-reported input, output, reasoning and cached-input totals under the workspace's `usage/` directory. These are telemetry, not an invoice. Missing fields remain unknown, and failed or interrupted requests may incur costs not present in the report. Provider billing is authoritative.

Local OCR does not send document images to an AI provider. An explicitly started model sample does call the chosen provider and can incur cost, even though its source is synthetic.

## References

- [DeepSeek thinking controls](https://api-docs.deepseek.com/guides/thinking_mode/)
- [DeepSeek prefix caching](https://api-docs.deepseek.com/guides/kv_cache/)
- [DeepSeek response and usage fields](https://api-docs.deepseek.com/api/create-chat-completion/)
