# Contributing

Contributions are welcome, especially additional synthetic fixtures, parsers for documented
health-export formats, and statistical hardening.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check .
pytest
```

Keep runtime code on the Python standard library unless a dependency clearly reduces security or
correctness risk. Tests must use synthetic health data; never commit a real export, repository
name, device identifier, or health value belonging to another person.

Analysis changes must remain deterministic, expose their method, require adequate baseline data,
and preserve the phrase “association, not causation” in user-facing reports.
