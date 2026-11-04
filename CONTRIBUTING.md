# Contributing

Thank you for your interest in MLX Media Workstation. Bug reports, fixes, documentation changes and tests are
welcome.

## Before you start

- Search the open and closed issues first. Your topic can already have an answer.
- Open an issue before you start a large change, a new tool or a new model. Then we can agree on the approach first.
- Small fixes (typos, clear bugs, a missing test) can go directly to a pull request.
- Report a security problem in private. Do not open a public issue. Read [SECURITY.md](SECURITY.md).

## Set up the development environment

You need an Apple Silicon Mac, Homebrew `python@3.12` and [uv](https://docs.astral.sh/uv/). The tests do not need a
model, `ffmpeg` or a network connection.

1. Fork the repository and clone your fork.
2. Make the app's environment and install the development tools:

   ```sh
   uv venv --python /opt/homebrew/bin/python3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
   ```

3. To run the app with real models, follow "Install the models" in [README.md](README.md).

## Run the checks

Run all three checks before you open a pull request:

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
```

The GitHub workflow in `.github/workflows/ci.yml` is turned off. The checks run only on your Mac, so a pull request
must say that they passed.

## Tests

- Add a test for each bug fix and each new behaviour.
- A test must not load a model, use the network or show a macOS notification. Use the fakes:
  `tests/fake_llm_worker.py` (the local LLM), `tests/fake_workstation.py` (the job queue for the agent tools) and the
  fixtures in `tests/conftest.py`.
- Runner output parsers have real logs in `tests/fixtures/`. Remove user names and home folder paths from a new log
  before you add it. The existing logs use `/opt/mlx-media` and `/opt/workstation`.
- Do not make a test weaker to get it to pass. Fix the cause.

## Code style

- Python 3.12. `ruff` settings are in `pyproject.toml`. Follow the style of the code around your change.
- Keep docstrings and comments short and plain.
- Keep the scope small. Do not reformat or move code that your change does not need.
- The web UI is plain HTML, CSS and JavaScript modules, with no build step and no framework. The page loads nothing
  from the network. Follow the rules in [web/README.md](web/README.md).
- Do not add a dependency without a good reason. Pin its exact version in `requirements.txt` or
  `requirements-dev.txt`.

## Documentation

Update the documents that your change affects in the same pull request:

| Change | Document |
|---|---|
| An endpoint or its response | `docs/API.md` |
| An agent tool, the approval policy or the sandbox | `docs/AGENT.md` |
| A `config.toml` key | `config.example.toml` and `README.md` |
| A model, its revision, size, peak memory or licence | `app/models.py` and `README.md` |
| Prompt rules for a tool | `docs/PROMPTING.md` |

Write documents in plain English: one idea in each sentence, short sentences, active voice, and numbered steps for
procedures.

## Models

A new model or a new revision of a model needs:

1. A pinned revision (a commit hash, not a branch name).
2. The download size and the peak memory of a real job, measured on your Mac. Say which Mac you used.
3. The licence name and a link to the licence text. Say whether the licence limits commercial use.

Do not commit model weights or generated outputs.

## Commits and pull requests

- Write commit messages in English in the [Conventional Commits](https://www.conventionalcommits.org/) form:
  `type(scope): summary`, for example `fix(jobs): keep the queue running when a write fails`. Use `feat`, `fix`,
  `docs`, `test`, `refactor`, `perf`, `style` or `chore`.
- Write the summary in the imperative mood, with at most 72 characters.
- Put one logical change in each commit.
- Fill in the pull request template: what changed, why, and how you tested it.
- For a UI change, add screenshots in the light and the dark theme.

## Licence

This project uses the MIT licence. When you contribute, you agree that your contribution uses the same licence.
Do not add code, fonts, icons or images from other projects unless their licence allows it. Include their licence
notice.
