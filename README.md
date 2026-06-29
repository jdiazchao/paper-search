# Paper Search

Paper Search is an Agent Skill for discovering and triaging academic papers in a
private, Tinder-style local interface. An AI agent searches for papers and
publishes a small candidate deck; the user swipes through it, and Paper Search
retains the feedback so later recommendations improve.

There is no recommendation service, account, API key, or hosted backend. The
site runs only on the user's machine at `127.0.0.1`.

## Install in Codex

Paper Search is distributed as the `paper-search` plugin through this repository's
marketplace:

```bash
codex plugin marketplace add jdiazchao/paper-search
codex plugin add paper-search@paper-search
```

Restart Codex or start a new thread, then invoke `$paper-search` or ask Codex
to find papers on a topic.

To update:

```bash
codex plugin marketplace upgrade paper-search
codex plugin add paper-search@paper-search
```

## Install as a standalone Agent Skill

For agents that support the [Agent Skills specification](https://agentskills.io),
clone the repository and expose the `paper-search` directory to the agent:

```bash
git clone https://github.com/jdiazchao/paper-search.git \
  ~/.local/share/paper-search
mkdir -p ~/.agents/skills
ln -s ~/.local/share/paper-search/plugins/paper-search/skills/paper-search \
  ~/.agents/skills/paper-search
```

Agents with a different skill directory can copy or link:

```text
plugins/paper-search/skills/paper-search/
```

## How it works

```text
SEARCH → PUBLISH → SWIPE → COMPACT FEEDBACK → REFINE → repeat
```

1. The agent finds and verifies candidate papers.
2. It publishes them to the bundled Python server.
3. The user likes or dislikes papers in the local browser interface.
4. Paper Search stores compact feedback and a liked-paper library.
5. The agent uses that signal to improve the next search.

The normal agent path uses `new-round --ensure-server --open`. Link verification
checks arXiv identifiers and titles before publishing.

## Requirements

- Python 3.8+
- A modern browser
- An agent that supports Agent Skills

Paper Search uses only Python's standard library. For HTTPS verification, it tries
Python's trust store, an optional installed `certifi` bundle, and common
operating-system CA bundles. Custom or enterprise certificate authorities can
be configured through `SSL_CERT_FILE` or `SSL_CERT_DIR`.

## Try it manually

```bash
SKILL_DIR=plugins/paper-search/skills/paper-search

python3 "$SKILL_DIR/paper_search.py" new-round \
  --candidates "$SKILL_DIR/examples/candidates.example.json" \
  --state .paperarena \
  --verify-links \
  --ensure-server \
  --open

python3 "$SKILL_DIR/paper_search.py" feedback \
  --state .paperarena \
  --compact
```

## Development

Run the tests:

```bash
python3 -m unittest discover -s tests -v
```

Validate the plugin:

```bash
python3 /path/to/plugin-creator/scripts/validate_plugin.py \
  plugins/paper-search
```

Runtime state remains under `.paperarena/` for compatibility with existing
saved libraries and is ignored by Git.

## Repository layout

```text
.agents/plugins/marketplace.json       Codex marketplace
plugins/paper-search/
  .codex-plugin/plugin.json            Plugin manifest
  skills/paper-search/
    SKILL.md                            Agent workflow
    paper_search.py                     Local server and CLI
    ui/                                 Swipe interface
tests/                                  Regression tests
```

## License

MIT
