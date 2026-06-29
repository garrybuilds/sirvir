# Lane-based Cost Model

Use this reference when reconstructing or explaining the budget model after the three-tier routing split.

## Core routing split

The recommended fleet routing policy defines three tiers:

### Premium tier
- primary: GLM 5.2 (via Nous)
- alternate premium reasoning lane: Qwen 3.7 MAX (via Nous)

Use premium for:
- high-stakes reasoning
- harder coding
- tasks where quality clearly outweighs cost
- orchestrator profiles whose mistakes cascade downstream

### Default tier
- primary: DeepSeek V4 Pro (provider of your choice)
- This is the smart middle lane — good quality without premium-by-default cost

Use default for:
- serious production work
- infra/model management
- implementation lanes needing good reasoning

### Cheap tier
- primary: DeepSeek V4 Flash (via your cheap provider)
- primary vision/aux: MiniMax M3 (via NVIDIA NIM, free)

Use cheap for:
- routine reasoning
- lower-stakes tasks
- high-volume coordination/admin work
- most vision/aux tasks
- comms, formatting, low-stakes drafting

## Cost-model consequence

With this three-tier split, the budget model is:

- **Cheap-tier traffic is effectively free** when it stays on NIM (vision/aux) or DeepSeek V4 Flash (text)
- **Default-tier traffic is low-cost** — DeepSeek V4 Pro is materially cheaper than GLM 5.2
- **Premium-tier traffic is the primary spend driver** — GLM 5.2 at $0.95/$3.00 per 1M tokens

This means monthly spend is governed mainly by:
1. premium-tier share (what fraction of total traffic hits GLM 5.2)
2. default-tier share (what fraction hits DeepSeek V4 Pro vs free Flash)
3. cache reuse / effective cost per 1M tokens

## Why the budget ladder still works

The preserved ladder was:
- optimistic: $0.11 / 1M
- base: $0.125 / 1M
- conservative: $0.14 / 1M

That ladder only remains plausible if:
- a large fraction of traffic stays on the free cheap tier (NIM + Flash)
- default-tier usage is moderate
- premium usage is selective rather than default
- caching is materially better than the pre-upgrade baseline

## Practical interpretation

- If premium share stays under ~30%, the fleet is more likely to remain near the healthy/base budget band.
- If premium share rises into the 40-50% range, cost pressure will drift toward the conservative band.
- If premium share rises above 50%, cost pressure will exceed the conservative band.
- If default-tier share is high but premium is low, costs stay moderate.
- If Qwen 3.7 MAX becomes the default premium lane instead of GLM 5.2, cost pressure rises because Qwen is the more expensive premium option ($1.25/$3.75 vs $0.95/$3.00).

## Recommended default reading of the three-tier split

For budget reasoning, assume:
- GLM 5.2 = standard premium lane (highest cost, highest quality)
- DeepSeek V4 Pro = default middle lane (moderate cost, good quality)
- DeepSeek V4 Flash = cheap text lane (lowest cost, acceptable quality)
- MiniMax M3 = free vision/aux lane (zero cost, good multimodal)

## Decision rule

When explaining why spend changed, separate the causes:
1. Did premium-tier share rise?
2. Did default-tier share rise (pushing more traffic off the free cheap lane)?
3. Did cache performance worsen?
4. Did vision/aux work drift off free MiniMax/NIM routing?

Do not explain spend growth from raw token volume alone when the routing mix changed materially.

