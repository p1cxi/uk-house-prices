# Qwen tool-calling eval harness

A golden-question suite for the **MCP / local-model path** of the analytics agent — the
path the llama.cpp chat UI drives, where there is **no deterministic `/ask` pre-router** and
tool + argument selection rests entirely on the tool descriptions. It exists so we can change
a tool description (or add a param) and *prove* the 8B still routes correctly and answers
honestly, instead of guessing.

## What it checks

For each case (`cases.py`): the model is given the analytics tool specs + a system prompt
mirroring the MCP server's honesty instructions, and we assert:

- **Routing** — did it pick the expected tool (or answer with no tool)?
- **Arguments** — did it set the arguments that matter (e.g. `area_scope=london`, `sort=ppsqm`,
  `candidate_floor_area=70`, `new_build=new`)? Extra args are ignored; an expected value of
  `None` means "should be absent", a list means "any of".
- **Answer honesty** (`e2e` cases) — execute the tool, feed the result back, get the final
  prose, and assert properties (e.g. a "this month" answer carries the registration-lag caveat;
  a bedroom request never claims results were filtered by bedrooms).

## Running

Stdlib only. Talks HTTP to the live LLM server and the live API (both must be reachable).
Defaults read `LLM_HOST` / `LLM_MODEL` from `./.env` if present.

```bash
python3 evals/run_evals.py                       # all cases, 1 sample each, deployed specs
python3 evals/run_evals.py --repeat 5            # 5 samples/case, report pass-rate (catch flakiness)
python3 evals/run_evals.py --case london_scope_followup -v
python3 evals/run_evals.py --llm http://192.168.10.11:8080 --api http://localhost:8003
```

Exit code is non-zero if any case's pass-rate is below `--threshold` (default `1.0`), so it
can gate a merge / CI step.

### Testing un-deployed wording (`--specs`)

By default specs come from `GET /api/analysis/tools` — i.e. the **deployed** image, which can
lag your working tree. To evaluate description/param changes *before* deploying, dump the
current source specs and point `--specs` at them:

```bash
docker compose run --rm --no-deps -v "$PWD/api:/api:ro" -w / api python -c \
 "import json; from api.analysis.tools import TOOLS; print(json.dumps({'tools':[{'name':t.name,'description':t.description,'parameters':t.parameters} for t in TOOLS]}))" \
 > evals/specs.current.json            # (or: evals/dump_specs.py in any env with the api deps)

python3 evals/run_evals.py --specs evals/specs.current.json
```

`specs.current.json` is generated, not committed (gitignored).

## Fidelity & limits (read before trusting a green run)

- The harness exercises **(model + tool specs + our system prompt)** at `temperature=0`. The
  real chat UI may prepend a **different** system prompt and use a different temperature / tool-call
  history format. So a green run proves the *descriptions* are sufficient under controlled
  conditions — it is not yet a byte-exact replay of the chat UI.
- Concretely: the live regression that motivated this harness ("anything closer to London?" →
  failed to set `area_scope=london`) **did not reproduce here even on the old wording**, which
  points at a chat-UI-environment difference (system prompt / sampling) rather than the
  description alone. Closing that gap — capturing the chat UI's actual system prompt + sampling
  and feeding them in — is the next fidelity step.
- Answer-honesty regexes are necessarily lenient; treat e2e failures as "look at this", and
  routing/argument failures as hard.

## Adding cases

Append to `CASES` in `cases.py`. Keep `expect_args` to the arguments that genuinely matter.
Every description change or new param should arrive with the case that locks its behaviour in.
