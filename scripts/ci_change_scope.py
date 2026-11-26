"""Decide whether a repository's test suite must run for a CI event.

A GitHub Actions job runs this script in the caller repo's checkout (full
history fetched). It appends ``code=true`` (run the suite) or ``code=false``
(skip it) to the file named by ``--output`` and prints one reason line.

The suite is skipped in two cases:

1. The change range touches only ``CHANGES.md``.
2. A pull request was synchronized only because its base branch moved. The PR
   fingerprint is the SHA-256 of the raw bytes of ``git diff --raw
   --no-renames --no-abbrev -z`` from the merge base to the head, excluding
   ``CHANGES.md``. The raw output holds the mode, both blob ids, the status and
   the path of every changed file, so the fingerprints match only when every
   changed file has identical content on both sides. The skip also needs the
   previous run to have succeeded.

Any doubt or error means ``code=true``. Every sha must be exactly 40 lowercase
hex characters, and an all-zero sha counts as unusable. The exit code is 0
except for argument errors.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys

CHANGES_FILE = "CHANGES.md"
SHORT_SHA_LENGTH = 7
GIT_TIMEOUT_SECONDS = 120
FAILED_RETURN_CODE = 128
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
FALLBACK_REASON = "decision failed; running the test suite"


def run_git(args, cwd=None):
    """Run a git command and return the completed process.

    Parameters:
        args (list[str]): Arguments after ``git``.
        cwd (str | Path | None): Directory to run in; defaults to the current one.

    Returns:
        subprocess.CompletedProcess: Result with captured bytes output. A command that
        times out or cannot start gives a non-zero return code and empty output. It
        never raises.
    """
    command = ["git", *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(command, FAILED_RETURN_CODE, b"", b"")


def is_usable_sha(sha):
    """Return True when the sha is 40 lowercase hex characters and not all zeros."""
    return bool(SHA_PATTERN.fullmatch(sha)) and set(sha) != {"0"}


def changed_files(git, diffRange):
    """Return the changed file names for a range, or None when git fails."""
    result = git(["diff", "--name-only", "-z", "--no-renames", diffRange])
    if result.returncode != 0:
        return None
    names = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [name for name in names if name]


def changes_md_only(git, event, baseSha, headSha, before, after):
    """Return True when the event range changes CHANGES.md and nothing else."""
    if event == "pull_request":
        first, last, separator = baseSha, headSha, "..."
    else:
        first, last, separator = before, after, ".."
    if not is_usable_sha(first) or not is_usable_sha(last):
        return False
    files = changed_files(git, f"{first}{separator}{last}")
    return bool(files) and all(name == CHANGES_FILE for name in files)


def has_commit(git, sha):
    """Return True when the commit exists locally."""
    return git(["cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0


def ensure_commit(git, sha):
    """Return True when the commit exists locally, fetching it from origin if needed."""
    if has_commit(git, sha):
        return True
    git(["fetch", "--no-tags", "origin", sha])
    return has_commit(git, sha)


def raw_pr_diff(git, rev, baseSha):
    """Return the raw PR code diff of a revision against the base, or None on git failure.

    The diff runs from the merge base and excludes CHANGES.md. The raw output lists
    modes, both blob ids, status and path for each changed file.
    """
    mergeBase = git(["merge-base", rev, baseSha])
    if mergeBase.returncode != 0:
        return None
    diff = git(
        [
            "diff",
            "--raw",
            "--no-renames",
            "--no-abbrev",
            "-z",
            mergeBase.stdout.decode("ascii", errors="replace").strip(),
            rev,
            "--",
            ".",
            f":(exclude){CHANGES_FILE}",
        ]
    )
    return diff.stdout if diff.returncode == 0 else None


def fingerprint(rawDiff):
    """Return the SHA-256 hex digest of the raw diff bytes."""
    return hashlib.sha256(rawDiff).hexdigest()


def base_move_only(git, baseSha, before, after, priorConclusion):
    """Return True when the PR code diff is unchanged since a run that passed."""
    if not (is_usable_sha(baseSha) and is_usable_sha(after)):
        return False
    if priorConclusion != "success" or not ensure_commit(git, before):
        return False
    afterDiff = raw_pr_diff(git, after, baseSha)
    if not afterDiff:
        return False
    beforeDiff = raw_pr_diff(git, before, baseSha)
    if beforeDiff is None:
        return False
    return fingerprint(beforeDiff) == fingerprint(afterDiff)


def decide_scope(git, event, action, baseSha, headSha, before, after, priorConclusion):
    """Decide whether the test suite must run.

    Parameters:
        git (callable): Runs git with a list of arguments and returns a completed process.
        event (str): ``pull_request`` or ``push``.
        action (str): The pull request action, or an empty string.
        baseSha (str): Pull request base sha, or an empty string.
        headSha (str): Pull request head sha, or an empty string.
        before (str): Sha before the push, or an empty string.
        after (str): Sha after the push, or an empty string.
        priorConclusion (str): Conclusion of the previous run, or an empty string.

    Returns:
        tuple[bool, str]: Whether to run the suite, and a one-line reason.
    """
    if changes_md_only(git, event, baseSha, headSha, before, after):
        return False, f"only {CHANGES_FILE} changed; skipping the test suite"
    isBaseMoveCandidate = (
        event == "pull_request" and action == "synchronize" and is_usable_sha(before)
    )
    if isBaseMoveCandidate and base_move_only(
        git, baseSha, before, after, priorConclusion
    ):
        shortSha = before[:SHORT_SHA_LENGTH]
        return False, (
            f"only the base moved; PR code diff unchanged since {shortSha}, "
            "which passed; skipping the test suite"
        )
    return True, "code changes need the test suite; running it"


def parse_args(argv):
    """Parse the command line arguments."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("--event", required=True, choices=["pull_request", "push"])
    parser.add_argument("--action", default="")
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--before", default="")
    parser.add_argument("--after", default="")
    parser.add_argument("--prior-conclusion", default="")
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv=None, git=run_git):
    """Run the decision, append ``code=...`` to the output file, and print the reason.

    Any exception while deciding is reported on stderr and falls back to running the
    suite, so the output file always receives a ``code=`` line.
    """
    args = parse_args(argv)
    try:
        shouldRun, reason = decide_scope(
            git,
            event=args.event,
            action=args.action,
            baseSha=args.base_sha,
            headSha=args.head_sha,
            before=args.before,
            after=args.after,
            priorConclusion=args.prior_conclusion,
        )
    except Exception as error:  # noqa: BLE001 - FAIL SAFE AT THE BOUNDARY
        print(f"ci_change_scope: {error!r}", file=sys.stderr)
        shouldRun, reason = True, FALLBACK_REASON
    with open(args.output, "a", encoding="utf-8") as handle:
        handle.write(f"code={'true' if shouldRun else 'false'}\n")
    print(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
