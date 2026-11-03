# Agent mode

## Settings

The `[agent]` section of `config.toml` sets the sandbox interpreter of `run_python` and the sources of `web_search`.
Settings > Agent edits it (in both profiles). The app reads it once, at start, when it builds the agent tools. A
change takes effect after a restart; the Settings screen shows the restart note until then.

| Key | Default | Meaning |
|---|---|---|
| `python` | `""` | The interpreter `run_python` runs in the sandbox. Empty = the app's own base Python with `-I -S`: the standard library only. |
| `searxng_url` | `""` | The base URL of a SearXNG instance you run yourself, with the JSON format on. `web_search` asks it first. Empty = not used. |
| `wikipedia_language` | `"en"` | The Wikipedia that `web_search` asks. |
| `marginalia` | `true` | `web_search` also asks Marginalia Search with the shared key `"public"`. That key is often rate limited. |

### Rules

The Settings screen (`PUT /api/settings`, see [API.md](API.md#settings)) refuses a value that breaks these rules:

- `python`: `""`, or an absolute path ("~" is expanded) to an executable file that is there now.
- `searxng_url`: `""`, or an `http://` or `https://` URL with a host. It cannot hold a user name or password, a query
  (`?`) or a fragment (`#`).
- `wikipedia_language`: a language code: two or three lowercase letters, then an optional `-` and lowercase letters
  (`en`, `de`, `pt-br`). The screen offers en, tr, de, fr, es, it, ja and zh. Another code works when you set it in
  the file by hand.
- `marginalia`: `true` or `false`.
- A text value cannot hold a double quote, a backslash or a line break, and is at most 1024 characters.

At start the app checks the same rules, except that `python` may be a relative path (counted from the app folder) and
need not exist: a bad value stops the start with a message. When the `python` file is missing, the agent has no
`run_python` tool.

### A sandbox environment with packages

The default sandbox Python has the standard library only. For numpy, pandas or matplotlib, make a separate
environment for the sandbox. Do these steps in Terminal, with [uv](https://docs.astral.sh/uv/):

1. Make the environment:

   ```sh
   uv venv --python 3.12 ~/mlx-media/venv-sandbox
   ```

2. Install the packages:

   ```sh
   uv pip install --python ~/mlx-media/venv-sandbox/bin/python numpy==2.5.4 pandas==3.1.0 matplotlib==3.11.2
   ```

3. In Settings > Agent, enter `~/mlx-media/venv-sandbox/bin/python` as the sandbox Python and press Save.
4. Restart the app.

The sandbox runs this interpreter with `-I`: its own packages work, but the user site folder and the `PYTHON*`
variables do not. The sandbox may read the environment's folder.
