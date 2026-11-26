# Changes

* **FEATURE**: add the `change-scope.yml` reusable workflow and its `scripts/ci_change_scope.py` decision script; the workflow finds the calling workflow from `github.workflow_ref` and sets the output `code` to `false` when a change touches only `CHANGES.md`, or when a push to a pull request only moved its base and the caller's run on the previous head passed, and to `true` otherwise or in any doubt.
