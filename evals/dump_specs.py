#!/usr/bin/env python3
"""Dump the CURRENT working-tree tool specs to JSON, so the eval can validate
un-deployed tool wording (the live API may lag the working tree).

Emits the same {name, description, parameters} shape as GET /api/analysis/tools.
Needs the api deps (psycopg, jsonschema), so run it where they're installed —
typically inside the api image with the working tree mounted over /api:

  docker compose run --rm --no-deps -v "$PWD/api:/api:ro" -w / api \
      python /api/../evals/dump_specs.py > evals/specs.current.json

  # or, without mounting evals/, inline:
  docker compose run --rm --no-deps -v "$PWD/api:/api:ro" -w / api python -c \
    "import json; from api.analysis.tools import TOOLS; \
     print(json.dumps({'tools':[{'name':t.name,'description':t.description,'parameters':t.parameters} for t in TOOLS]}))" \
    > evals/specs.current.json

Then:  python3 evals/run_evals.py --specs evals/specs.current.json
"""
import json
import sys

try:
    from api.analysis.tools import TOOLS
except ImportError:
    sys.path.insert(0, "/")
    from api.analysis.tools import TOOLS

specs = [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in TOOLS]
print(json.dumps({"tools": specs}, indent=2))
