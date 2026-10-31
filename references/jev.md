# Jev (OpenRouter Decisions API): what chat-miner relies on

Endpoint: `POST https://openrouter.ai/api/alpha/decisions`
Headers: `Authorization: Bearer $OPENROUTER_API_KEY`, `Content-Type: application/json`
Model: `typesafe/jev-1.13` (dated snapshots such as `typesafe/jev-1.13-20260917` appear in responses).

Request body:
```json
{
  "model": "typesafe/jev-1.13",
  "state": {"message": "text to classify"},
  "questions": {
    "name": {"type": "noul", "instructions": "...", "criteria": {"true": "...", "false": "..."}},
    "other": {"type": "choice", "instructions": "...", "criteria": {"a": "...", "b": "..."}}
  }
}
```

Question types used here:
- `noul`: yes/no. Response field `noul` is the probability of yes, 0..1.
- `choice`: one option from `criteria`. Response has `choice`, `confidence` and `probabilities`.
- `score`: ordered scale. Not used by chat-miner.

Response fields used by chat-miner:
- `answers[name].noul` (noul) or `answers[name].probabilities` (choice)
- `usage.input_tokens`, `usage.output_tokens`, `usage.cost` (USD, per call)
- `model` (dated snapshot that served the call)

Billing: input tokens are billed, output tokens are free according to the docs.
The docs do not list the price per million tokens on the page we could read; use `usage.cost` from responses as the source of truth.
One observed response: 476 input tokens, cost 0.000019992 USD.

Limits: 262,144 tokens of context. Requests above about 32,000 bytes of text, and any request with an image, run in TypeSafe's long-context mode (slower, less accurate per the docs). Chat messages are short, so this normally does not apply.

Threshold guidance from the docs: confidence describes how concentrated the distribution is, not whether an answer is safe. Pick thresholds from labelled examples, not from a round number. chat-miner's `eval` step is not built yet: label 100 to 150 messages by hand before trusting a threshold.
