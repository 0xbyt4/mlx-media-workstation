# Security policy

## Supported versions

Only the latest commit on the `main` branch gets security fixes. There are no release branches.

## Report a vulnerability

Do not report a security problem in a public issue, a pull request or a discussion.

1. Open the repository's **Security** tab on GitHub.
2. Select **Report a vulnerability**. This sends a private report that only the maintainers can see.
3. Write what the problem is, the steps to reproduce it, and what an attacker can do with it.
4. Add the app version and commit from Settings > About, your macOS version and your Mac's chip.

You get an answer within 14 days. When the fix is ready, we publish an advisory and credit you, unless you ask us
not to.

## Scope

Workstation is a local app. The server listens on `127.0.0.1` only and needs the session cookie of the current run.
These problems are in scope:

- Another program, another user on the Mac or a web page in a browser can use the API without the session cookie.
- A web page can read data from the app or send jobs to it.
- In an agent chat, model-written code leaves the sandbox: it uses the network, or it reads or writes files outside
  the folders that `docs/AGENT.md` allows.
- An agent tool runs without the approval that `docs/AGENT.md` says it needs.
- An uploaded file or a file name lets an attacker read or write a file outside the app's folders.

These problems are not in scope:

- The limits that `docs/AGENT.md` lists in "What is not protected".
- An attacker who already runs code as your user on the Mac.
- Problems in a model, its weights or its runtime (mflux, mlx-audio, ACE-Step, ltx-2-mlx, mlx-vlm). Report them to
  that project.
- The content that a model generates.
