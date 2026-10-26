# Third-party notices

## CoverUp

Source: https://github.com/plasma-umass/coverup

Version: v0.6.3; commit: c4ad2f13c74c1154761038cf924ae5ece27dffdc.

Authors credited by upstream: Juan Altmayer Pizzorno and Emery Berger.

License: Apache License, Version 2.0. The original license is retained in the project root and `src/llm_test_tool/_vendor/coverup/LICENSE`.

The following source modules were extracted from the upstream `src/coverup/` directory and retained in `src/llm_test_tool/_vendor/coverup/`:

- `segment.py`: AST-based missing-coverage segment selection, enclosing function/class selection, context and import extraction.
- `codeinfo.py`: AST parsing and source context/import utilities.
- `utils.py`: helpers needed by segment formatting. The optional upstream SlipCover summary function is not used by this project.

Local modifications: `segment.py` and `codeinfo.py` use `tokenize.open` when reading Python source to honor encoding cookies. Package-relative imports now resolve under the vendored package. `__init__.py` is local scaffolding. `utils.py` is unchanged.

The main controller, blackbox/whitebox prompts, chat-completions client, coverage.py adapter, isolated worker, defect assessment, categorized repair, strict regression checks, CLI and reports are additions/reimplementations. This is a selective derivative of CoverUp, not a full upstream fork, and does not claim upstream endorsement or unchanged upstream behavior.

Upstream's multi-provider dependency graph is not installed; LiteLLM and SlipCover are not runtime dependencies of this derivative. Dependencies used by this project retain their own licenses.

## boltons — sample target, not the tool's implementation

Source: https://github.com/mahmoud/boltons

Version: 25.0.0; commit: c23dbdadb6fecdf505eb4231559561913b84c13f.

Copyright (c) 2013, Mahmoud Hashemi. Licensed under the BSD 3-Clause terms retained in `targets/boltons/LICENSE` and the source file headers. The checkout is an unmodified target project used for future testing; its code is not included in the installable `llm_test_tool` package.

Redistributions must retain the upstream notices and license. Do not use contributor names to imply endorsement. On 2026-10-08 the selected mathutils module was tested in disposable local copies; accepted candidate fixes are retained in run workspaces and patches, and the original target checkout remains unchanged. Findings have not been confirmed by upstream maintainers.
