# Notice — provenance and attribution

## Origin of this work

**Hollow on Osaurus** is a clean-room reimplementation of
**[Hollow AgentOS](https://github.com/ninjahawk/hollow-agentOS)** by
**[ninjahawk](https://github.com/ninjahawk)**.

It was built in July 2026 from the project's public wiki documentation
([hollow-wiki](https://ninjahawk.github.io/hollow-wiki/) /
[source](https://github.com/ninjahawk/hollow-wiki)) only. **No source code and
no documentation text from the original project was copied.** The
implementation, all code, and all prose in this repository are original.

The concepts and mechanics, however, originate with the original author and
deserve full credit: the three-agent habitat (scout / analyst / builder), the
suffering/stressor model with capability locking and a guaranteed path out,
promoted environment lessons, the multi-layer goal-validation pipeline, the
Claude change-request bridge, and the operator API. If you find these ideas
interesting, please visit and star the
[original project](https://github.com/ninjahawk/hollow-agentOS).

What this port changes: all inference runs through
[Osaurus](https://github.com/osaurus-ai/osaurus) (Apple-Silicon-native MLX
server) via its OpenAI-compatible API instead of the original's
Ollama + Docker stack, and the substrate is a compact Python rewrite targeting
macOS only.

## Upstream licensing status

As of 2026-07-09, `ninjahawk/hollow-agentOS` contains no `LICENSE` file, but
the author's declared intent is MIT: `pyproject.toml` states
`license = {text = "MIT"}` with the OSI "MIT License" classifier, and the
README carries an MIT badge. The wiki repository carries no license. Because
this port copies no code or text from either repository, it is not a
derivative work of them; this repository is licensed independently under the
[MIT License](LICENSE), matching the original author's declared license.

## Third-party software (not bundled)

None of the following is vendored or redistributed in this repository; they
are runtime/install-time dependencies fetched by the user:

- [Osaurus](https://github.com/osaurus-ai/osaurus) — MIT License
- [httpx](https://github.com/encode/httpx) — BSD-3-Clause License
- [rich](https://github.com/Textualize/rich) — MIT License
