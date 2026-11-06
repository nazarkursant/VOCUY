# Contributing to VO Studio

VO Studio is intentionally small: a Python standard-library server and a plain
HTML, CSS and JavaScript interface. Keep setup simple and changes focused.

## Run locally

```sh
python3 server.py
```

On Windows, use `py -3 server.py`. The server opens a browser automatically and
binds to `127.0.0.1`. There are no packages to install or frontend build steps.

## Check a change

```sh
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check static/playback.js
node --test tests/test_playback.js tests/test_export_ui.js
```

Node.js is optional for development; it is only used here to check JavaScript
syntax. The Python tests fake ElevenLabs responses and use temporary files.
The export checks also execute generated JSX against an After Effects API test
double when Node.js is available; they do not replace a smoke test in After Effects.
They need permission to open a temporary localhost port, but no API key or
internet access. GitHub Actions runs the same checks.

For interface changes, check both a wide desktop window and a narrow mobile
window. Use fictional project names and scripts in screenshots. Keep API keys,
private voice IDs, generated media, transcripts, `.env` files and machine-specific
paths out of commits. Review `git diff --cached` before pushing.

## Send a change

Open an issue for a bug or a focused pull request for a fix. Describe the behavior,
how to reproduce it and how you checked the change. Use mocked API responses in
tests so contributors do not need paid generation calls.

Keep the local-only server binding, request-origin checks and credential redaction
intact. See [Privacy and data flow](docs/PRIVACY.md) before changing persistence or
error handling.
