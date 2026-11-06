# Validation record

Date: 2026-11-06. Platform: Windows, Python 3.11.8.

- Official `mcp` 1.30.0 and Pydantic 2.14.3; dependency versions recorded in `requirements.lock.txt`.
- `python -m unittest discover -s tests -q`: **26 tests passed**.
- Real official MCP client connected over stdio and exercised all six tools, two resources and the mentor prompt. The isolated server was launched from a different working directory.
- Persistent proposal gate, complete probe answers, A/B automatic preparation, C/D scoring, sequential dependencies, task reopening protection, atomic rollback, concurrent retry idempotency, backup integrity, domain filters and vault recovery passed.
- `pip check`: **No broken requirements found**.
- Mermaid **11.17.2** parsed four diagrams: empty relationship graph, empty mindmap, relationship graph with hostile labels, and corresponding mindmap. Parsing used a local jsdom 26.1.0 environment without uploading content.
- Python AST/token inspection found no `pass` statements or unfinished `TODO` comments in the three implementation modules.
- Default workspace initialized with **zero tasks and zero knowledge nodes**. SQLite integrity check returned `ok`; startup daily backup and both Markdown files exist.

The source archive excludes runtime databases, backups, caches and test dependencies. Existing local data stays in the project directory.
Host answer authenticity and completeness of LLM-generated prerequisite lists are outside the server's independently verifiable boundary; runtime validation enforces the stored gate and schedule invariants.
