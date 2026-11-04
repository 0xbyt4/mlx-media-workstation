"""Make the README and guide screenshots (docs/images/*.png) from a demo state, with no model and no download.

The steps:

1. scripts/demo_data.py fills a new outputs folder in a temporary demo home.
2. Stand-in model files go into the demo home, so every tool counts as installed. A stand-in mflux command prints a
   progress bar and writes nothing.
3. The app starts on a free port with that demo home ($HOME) and outputs folder, the fake LLM worker of
   tests/fake_llm_worker.py (driven by demo_data.agent_turn) and the fake web and sandbox of the agent tests.
4. Headless Google Chrome, with its own profile in the demo home, signs in with the token from the demo outputs
   folder (never printed), queues the live jobs, asks the agent chat for a second answer and saves each view.

Paths of the demo home show as /Users/demo, and the repository as /Users/demo/mlx-media-workstation. The script stops
when a view still shows a path of this Mac or the user's name. It never touches the repository's outputs/ or
config.toml. Run it from the repository folder; it takes about a minute:

  .venv/bin/python scripts/screenshots.py              # all views into docs/images/
  .venv/bin/python scripts/screenshots.py --only chat  # one view
  .venv/bin/python scripts/screenshots.py --keep       # keep the demo folder and print its path
"""

import argparse
import base64
import getpass
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
WIDTH, HEIGHT, SCALE = 1440, 900, 2
SHOWN_HOME = "/Users/demo"
SHOWN_ROOT = SHOWN_HOME + "/mlx-media-workstation"
# name, route, theme, what must be on the page before the capture (a CSS selector)
VIEWS = [
    ("create", "#/create?tool=video.t2v", "light", ".tool-head"),
    ("create-dark", "#/create?tool=video.t2v", "dark", ".tool-head"),
    ("jobs", "#/jobs", "light", "[role=progressbar]"),
    ("chat", "#/chat?id={workstation}", "light", ".msg-ai"),
    ("agent", "#/chat?id={agent}", "light", ".approval"),
    ("library", "#/library", "light", "video"),
    ("models", "#/models", "light", "main h2, main h3"),
    ("settings", "#/settings", "light", "main h2, main h3"),
]
# the stand-in MOSS-Image bar; its time left blends this rate with the demo's finished MOSS job (about 4 min)
MOSS_STEPS, MOSS_STEP_S = 50, 2.0


# ---------- the app (runs in a child process with the demo home) ----------


def fake_models() -> None:
    """Stand-in files for every model and runtime under the demo home, and an mflux MOSS-Image command that prints a
    tqdm bar for MOSS_STEPS steps."""
    from app import models

    for m in models.MODELS:
        root = models.target(m)
        if not m.files:  # a python path: the file itself
            root.parent.mkdir(parents=True, exist_ok=True)
            root.touch()
            continue
        for name in m.files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    lines = []
    for i in range(MOSS_STEPS + 1):
        done, left = i * MOSS_STEP_S, (MOSS_STEPS - i) * MOSS_STEP_S
        fill = "█" * (i * 10 // MOSS_STEPS)
        lines.append(
            f"{100 * i // MOSS_STEPS:3d}%|{fill:<10}| {i}/{MOSS_STEPS} "
            f"[{int(done) // 60:02d}:{int(done) % 60:02d}<{int(left) // 60:02d}:{int(left) % 60:02d}, {MOSS_STEP_S:.2f}s/it]"
        )
    body = "".join(
        f"printf '%s\\r' '{line}' >&2\nsleep {MOSS_STEP_S}\n" for line in lines
    )
    stub = models.PATHS.mflux_bin / "mflux-generate-moss-image"
    stub.write_text(
        f"#!/bin/sh\n# a stand-in for the demo screenshots: a progress bar, no image\n{body}exit 1\n"
    )
    stub.chmod(0o755)


def serve(home: Path, port: int) -> None:
    """The app on 127.0.0.1:port with the demo home, the fake LLM worker and the fake agent web and sandbox."""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from app import config

    # the demo's own config.toml path (missing: every default), before the other modules read config
    config.FILE = home / "mlx-media-workstation" / "config.toml"
    config.PATHS, config.ASSIST, config.APP = (
        config.load(config.FILE),
        config.load_assist(config.FILE),
        config.load_app(config.FILE),
    )
    fake_models()

    from types import SimpleNamespace

    import uvicorn

    from app import chat, llm, macos, server
    from app.agent import Limits, build_registry, load_settings
    from app.agent.web_tools import Net
    from tests.test_agent_chat import FakeNet, FakeSandbox

    out = home / "mlx-media-workstation" / "outputs"
    server.OUT, chat.CHATS, llm.LOG = out, out / "chats", out / "llm.log"
    server.SESSION_COOKIE = f"workstation_{port}"
    server.ORIGINS.add(f"http://127.0.0.1:{port}")
    llm.PATHS = SimpleNamespace(qwen_python=Path(sys.executable), qwen_model=Path("ok"))
    llm.WORKER = [str(Path(__file__).resolve()), "--llm-worker"]
    llm.installed = lambda: True

    class NoAwake:
        def sync(self, want: Any) -> bool:
            return False

        def release(self) -> None:
            pass

    server.awake = NoAwake()
    macos.notify = lambda *a, **k: None
    net, regs = FakeNet(), {}

    def registry() -> Any:
        if "r" not in regs:
            regs["r"] = build_registry(
                chat.port,
                chat.settings or load_settings(),
                net=Net(net.resolve, net.transport),
                sandbox=FakeSandbox(),
                limits=Limits(),
            )
        return regs["r"]

    chat.registry = registry
    server.start()
    print("ready", flush=True)
    try:
        uvicorn.Server(
            uvicorn.Config(
                server.app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                timeout_graceful_shutdown=3,
            )
        ).run()
    finally:
        server.shutdown()


def llm_worker() -> None:
    """tests/fake_llm_worker.py, with the demo agent answer for the user text in $DEMO_AGENT_TURN."""
    sys.path.insert(0, str(ROOT))
    from tests import fake_llm_worker as fake

    turn = json.loads(Path(os.environ["DEMO_AGENT_TURN"]).read_text())
    scripted = fake.scripted

    def demo(messages: list[dict[str, Any]], final: bool = False) -> Any:
        users = [i for i, m in enumerate(messages) if m["role"] == "user"]
        if users and fake.text_of(messages[users[-1]]).strip() == turn["text"]:
            messages = list(messages)
            messages[users[-1]] = {
                **messages[users[-1]],
                "content": fake.STEPS + json.dumps(turn["steps"]),
            }
        return scripted(messages, final)

    fake.scripted = demo
    sys.argv = [sys.argv[0], sys.argv[-1]]
    fake.main()


# ---------- the browser (Chrome DevTools protocol over a pipe) ----------


class Browser:
    """Headless Chrome driven over --remote-debugging-pipe (fd 3 in, fd 4 out): no port and no extra package."""

    def __init__(self, profile: Path) -> None:
        to_chrome_r, self.to_chrome = os.pipe()
        self.from_chrome, from_chrome_w = os.pipe()
        # Chrome reads fd 3 and writes fd 4: a shell moves the two pipe ends there, then becomes Chrome
        self.proc = subprocess.Popen(
            [
                "/bin/sh", "-c", f'exec "$0" "$@" 3<&{to_chrome_r} 4>&{from_chrome_w}',
                str(CHROME), "--headless=new", "--remote-debugging-pipe", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
                "--hide-scrollbars", "--force-color-profile=srgb", "--mute-audio",
                f"--window-size={WIDTH},{HEIGHT}", "about:blank",
            ],
            pass_fds=(to_chrome_r, from_chrome_w), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self.buf, self.n, self.session = b"", 0, None
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self.call(
            "Target.attachToTarget", {"targetId": target, "flatten": True}
        )["sessionId"]
        self.call("Page.enable")
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": WIDTH,
                "height": HEIGHT,
                "deviceScaleFactor": SCALE,
                "mobile": False,
            },
        )

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.n += 1
        msg: dict[str, Any] = {"id": self.n, "method": method, "params": params or {}}
        if self.session:
            msg["sessionId"] = self.session
        os.write(self.to_chrome, json.dumps(msg).encode() + b"\0")
        while True:
            while b"\0" not in self.buf:
                chunk = os.read(self.from_chrome, 1 << 20)
                if not chunk:
                    raise SystemExit("Chrome closed the connection.")
                self.buf += chunk
            raw, self.buf = self.buf.split(b"\0", 1)
            reply = json.loads(raw)
            if reply.get("id") == self.n:
                if "error" in reply:
                    raise SystemExit(
                        f"Chrome: {method}: {reply['error'].get('message')}"
                    )
                return reply["result"]

    def js(self, expr: str) -> Any:
        """The value of a page expression (a promise is awaited)."""
        r = self.call(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": True, "returnByValue": True},
        )
        if "exceptionDetails" in r:
            raise SystemExit(
                f"Page error: {r['exceptionDetails'].get('exception', {}).get('description')}"
            )
        return r["result"].get("value")

    def wait(self, expr: str, what: str, timeout: float = 30) -> None:
        end = time.monotonic() + timeout
        while not self.js(f"(async () => Boolean(await ({expr})))()"):
            if time.monotonic() > end:
                raise SystemExit(f"Timed out waiting for {what}.")
            time.sleep(0.25)

    def goto(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})
        self.wait("document.readyState === 'complete'", url)

    def theme(self, scheme: str) -> None:
        features = [
            {"name": "prefers-color-scheme", "value": scheme},
            {"name": "prefers-reduced-motion", "value": "reduce"},
        ]
        self.call("Emulation.setEmulatedMedia", {"features": features})

    def shot(self, path: Path) -> None:
        data = self.call(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
        )["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.session = None
            self.call("Browser.close")
            self.proc.wait(10)
        except (SystemExit, OSError, subprocess.TimeoutExpired):
            self.proc.terminate()
            self.proc.wait(10)


# ---------- the run ----------

# Replace the demo paths in the page's text, field values and titles; then return what still looks like a path of
# this Mac or the user's name (the screenshot is refused then).
SCRUB = """(() => {
  const pairs = %s, leaks = %s, found = new Set();
  const fix = (s) => { for (const [a, b] of pairs) s = s.split(a).join(b); return s; };
  const check = (s) => { for (const l of leaks) if (new RegExp(l).test(s)) found.add(s.slice(0, 120)); };
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    const v = fix(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v; check(v);
  }
  for (const el of document.querySelectorAll('input, textarea')) { el.value = fix(el.value); check(el.value); }
  for (const el of document.querySelectorAll('[title], [aria-label], [placeholder]')) {
    for (const a of ['title', 'aria-label', 'placeholder']) {
      const v = el.getAttribute(a); if (v !== null) { el.setAttribute(a, fix(v)); check(fix(v)); }
    }
  }
  return [...found];
})()"""
# Scroll the chat up to the start of the last answer, when its approval buttons stay on screen.
AGENT_SCROLL = """(() => {
  const box = document.querySelector('.msgs'), answers = document.querySelectorAll('.msg-ai');
  const keep = document.querySelector('.approval-actions');
  if (!box || !answers.length || !keep) return;
  const shift = answers[answers.length - 1].getBoundingClientRect().top - box.getBoundingClientRect().top - 12;
  if (shift < 0 && keep.getBoundingClientRect().bottom - shift <= box.getBoundingClientRect().bottom) box.scrollTop += shift;
})()"""
# Images loaded, and each video on screen that has a source shows its first frame (the Library sets a card's source
# only near the screen)
SETTLE = """(() => {
  const seen = (el) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight && r.width > 0; };
  const imgs = [...document.images].filter(seen).every((i) => i.complete);
  const vids = [...document.querySelectorAll('video')].filter((v) => seen(v) && v.currentSrc)
    .every((v) => v.readyState >= 2 || v.error);
  return imgs && vids;
})()"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def paths_of(p: Path) -> list[str]:
    """The path as written and as resolved (/tmp is /private/tmp on macOS)."""
    return sorted({str(p), str(p.resolve())}, key=len, reverse=True)


def optimise(path: Path) -> None:
    """Recompress a PNG losslessly with ffmpeg (best filter per row, zlib level 9) when that makes it smaller."""
    tmp = path.with_suffix(".min.png")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(path),
            "-pred",
            "mixed",
            "-compression_level",
            "9",
            str(tmp),
        ],
        check=True,
    )
    if tmp.stat().st_size < path.stat().st_size:
        tmp.replace(path)
    else:
        tmp.unlink()


def run(out_dir: Path, only: set[str], keep: bool) -> None:
    sys.path.insert(0, str(ROOT))
    from scripts import demo_data

    if not CHROME.exists():
        raise SystemExit(f"Google Chrome is not at {CHROME}.")
    demo = Path(tempfile.mkdtemp(prefix="mmw-demo-")).resolve()
    home = demo / "home"
    outputs = home / "mlx-media-workstation" / "outputs"
    server = browser = None
    log = demo / "server.log"
    try:
        print("Making the demo data...", flush=True)
        made = demo_data.build(outputs)
        text, steps = demo_data.agent_turn(made["jobs"])
        (demo / "agent_turn.json").write_text(
            json.dumps({"text": text, "steps": steps})
        )
        port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("HF_")}
        env.update(
            HOME=str(home),
            HF_HUB_OFFLINE="1",
            DEMO_AGENT_TURN=str(demo / "agent_turn.json"),
        )
        server = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve", str(home), str(port)],
            cwd=ROOT, env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
        )  # fmt: skip
        base = f"http://127.0.0.1:{port}"
        token_file = outputs / ".server-token"
        end = time.monotonic() + 60
        while not (token_file.exists() and "ready" in log.read_text()):
            if server.poll() is not None or time.monotonic() > end:
                raise SystemExit(f"The app did not start. Its log:\n{log.read_text()}")
            time.sleep(0.2)
        browser = Browser(demo / "chrome")
        browser.theme("light")
        query = urllib.parse.urlencode(
            {"t": token_file.read_text().strip(), "next": "/"}
        )
        browser.goto(
            f"{base}/auth?{query}"
        )  # the token goes to Chrome only, never to the output
        browser.wait("document.querySelector('nav, aside')", "the app")
        prepare(browser, made, text)
        browser.goto(
            f"{base}/"
        )  # a fresh page: the sidebar and the views start with the live jobs
        browser.wait("document.querySelector('nav, aside')", "the app")
        pairs = [[p, SHOWN_HOME] for p in paths_of(home)]
        pairs = [[p, SHOWN_ROOT] for p in paths_of(ROOT)] + pairs
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        user = getpass.getuser()
        leaks = [re.escape(p) for p in paths_of(Path.home())] + [
            rf"\b{re.escape(user)}\b",
            "/private/",
            "/var/folders/",
        ]
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, route, scheme, selector in VIEWS:
            if only and name not in only:
                continue
            browser.theme(scheme)
            browser.js(f"location.hash = {json.dumps(route.format(**made['chats']))}")
            browser.wait(
                f"document.querySelector({json.dumps(selector)})", f"{name}: {selector}"
            )
            time.sleep(1.5)
            if (
                name == "agent"
            ):  # start the view at the question when the approval card still fits under it
                for _ in range(
                    2
                ):  # again, when the chat's own scroll to the end came after the first one
                    browser.js(AGENT_SCROLL)
                    time.sleep(0.5)
            try:
                browser.wait(SETTLE, f"{name}: images and videos", 15)
            except SystemExit as e:
                print(f"Warning: {e} The screenshot is taken anyway.")
            found = browser.js(SCRUB % (json.dumps(pairs), json.dumps(leaks)))
            if found:
                raise SystemExit(
                    f"{name}: the page shows private text, no screenshot: {found}"
                )
            time.sleep(0.3)
            path = out_dir / f"{name}.png"
            browser.shot(path)
            optimise(path)
            print(
                f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: {path.stat().st_size // 1024} KB"
            )
        errors = server_errors(log)
        if errors:
            print("The app logged errors:\n" + errors)
    finally:
        if browser:
            browser.close()
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(60)
            except subprocess.TimeoutExpired:
                server.kill()
        stop_jobs(outputs)
        if keep:
            print(f"Demo folder kept: {demo}")
        else:
            shutil.rmtree(demo, ignore_errors=True)


def prepare(browser: Browser, made: dict[str, Any], agent_text: str) -> None:
    """Fill the Create form, queue the live jobs, ask the agent chat its second question and wait until the job runs
    and the approval card shows."""
    from scripts import demo_data

    browser.js(
        f"localStorage.setItem('ws.form.video.t2v', {json.dumps(json.dumps(demo_data.CREATE_FORM))});"
        "localStorage.setItem('ws.lastTool', '\"video.t2v\"');"
    )
    for job in demo_data.LIVE_JOBS:
        browser.js(
            f"""(async () => {{
              const f = new FormData();
              for (const [k, v] of Object.entries({json.dumps(job)})) f.append(k, v);
              const r = await fetch('/api/jobs', {{method: 'POST', body: f}});
              if (!r.ok) throw new Error('job: ' + r.status + ' ' + await r.text());
            }})()"""
        )
        time.sleep(1.2)  # the first job starts before the next ones arrive
    browser.js(
        f"""(async () => {{
          const f = new FormData();
          f.append('text', {json.dumps(agent_text)});
          f.append('effort', 'low');
          const r = await fetch('/api/chats/{made["chats"]["agent"]}/messages', {{method: 'POST', body: f}});
          if (!r.ok) throw new Error('message: ' + r.status + ' ' + await r.text());
          r.body.cancel();  // the answer goes on in the app; the chat view replays it
        }})()"""
    )
    browser.wait(
        "fetch('/api/jobs').then((r) => r.json()).then((j) => j.some((x) => x.status === 'running' && x.progress"
        " && x.progress.fraction > 0.25))",
        "the running job's progress",
        60,
    )


def stop_jobs(outputs: Path) -> None:
    """Stop a demo job's process group that a killed app left running (job.json keeps "pgid" while a job runs)."""
    for meta in outputs.glob("*/job.json"):
        try:
            pgid = json.loads(meta.read_text()).get("pgid")
        except (OSError, json.JSONDecodeError):
            continue
        if pgid:
            try:
                os.killpg(int(pgid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def server_errors(log: Path) -> str:
    lines = [
        line
        for line in log.read_text().splitlines()
        if re.search(r"error|exception|traceback", line, re.IGNORECASE)
    ]
    return "\n".join(lines[-20:])


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]), int(sys.argv[3]))
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--llm-worker":
        llm_worker()
        return
    parser = argparse.ArgumentParser(
        description="Make the screenshots in docs/images from a demo state."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs" / "images",
        help="folder for the PNG files",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="one view (repeat for more): " + ", ".join(v[0] for v in VIEWS),
    )
    parser.add_argument("--keep", action="store_true", help="keep the demo folder")
    args = parser.parse_args()
    unknown = set(args.only) - {v[0] for v in VIEWS}
    if unknown:
        parser.error(f"unknown view(s): {', '.join(sorted(unknown))}")
    run(args.out.resolve(), set(args.only), args.keep)


if __name__ == "__main__":
    main()
