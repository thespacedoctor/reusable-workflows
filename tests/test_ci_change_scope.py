"""Tests for scripts/ci_change_scope.py, run against real temporary git repos."""

import os
import subprocess
from functools import partial

import ci_change_scope
import pytest

ZEROS = "0" * 40
APP_V1 = "def app():\n    value = 1\n    return value\n"
APP_V2 = "def app():\n    value = 2\n    return value\n"
APP_INDENT = "def app():\n  value = 1\n  return value\n"
OTHER = "def other():\n    return 0\n"


@pytest.fixture(autouse=True)
def isolated_git_environment(monkeypatch, tmp_path):
    """Keep every git call independent of the host's git config and GIT_* variables."""
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))


class Repo:
    """A throwaway git repository with helpers for building PR histories."""

    def __init__(self, path):
        self.path = path
        path.mkdir()
        self.git("init", "-q", "-b", "develop")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.hooksPath", os.devnull)
        self.write("app.py", APP_V1)
        self.write("CHANGES.md", "# changes\n")
        self.commit("initial")

    def git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def write(self, name, text):
        content = text if isinstance(text, bytes) else text.encode()
        (self.path / name).write_bytes(content)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.sha()

    def sha(self, rev="HEAD"):
        return self.git("rev-parse", rev)

    def is_ancestor(self, ancestor, descendant):
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.path,
            check=False,
        )
        return result.returncode == 0

    def runner(self):
        return partial(ci_change_scope.run_git, cwd=self.path)

    def decide(self, **overrides):
        args = {
            "event": "pull_request",
            "action": "synchronize",
            "baseSha": "",
            "headSha": "",
            "before": "",
            "after": "",
            "priorConclusion": "success",
        }
        args.update(overrides)
        return ci_change_scope.decide_scope(git=self.runner(), **args)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo")


def base_moved_scenario(repo):
    """PR branch with one code commit, then develop gains a commit and is merged in."""
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("app.py", APP_V2)
    before = repo.commit("pr code change")
    repo.git("checkout", "-q", "develop")
    repo.write("other.py", OTHER)
    baseSha = repo.commit("develop moves")
    repo.git("checkout", "-q", "feature")
    repo.git("merge", "-q", "--no-edit", "develop")
    after = repo.sha()
    return {"baseSha": baseSha, "headSha": after, "before": before, "after": after}


def test_skips_when_only_base_moved_and_prior_run_succeeded(repo):
    scenario = base_moved_scenario(repo)

    shouldRun, reason = repo.decide(**scenario)

    assert shouldRun is False
    assert "only the base moved" in reason
    assert scenario["before"][:7] in reason


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", ""])
def test_runs_when_base_moved_but_prior_run_did_not_succeed(repo, conclusion):
    scenario = base_moved_scenario(repo)

    shouldRun, _ = repo.decide(priorConclusion=conclusion, **scenario)

    assert shouldRun is True


def test_runs_when_push_added_code_change_to_the_pr(repo):
    scenario = base_moved_scenario(repo)
    repo.write("app.py", APP_V2 + "\nEXTRA = True\n")
    after = repo.commit("more pr code")
    scenario.update(headSha=after, after=after)

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_pr_file_changed_only_by_indentation(repo):
    scenario = base_moved_scenario(repo)
    repo.write("app.py", APP_V2.replace("    ", "  "))
    after = repo.commit("reindent")
    scenario.update(headSha=after, after=after)

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_merge_conflict_was_resolved_by_editing_a_pr_file(repo):
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("app.py", APP_V2)
    before = repo.commit("pr change")
    repo.git("checkout", "-q", "develop")
    repo.write("app.py", APP_V1.replace("value = 1", "value = 3"))
    baseSha = repo.commit("conflicting develop change")
    repo.git("checkout", "-q", "feature")
    merge = subprocess.run(
        ["git", "merge", "--no-edit", "develop"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge.returncode != 0
    repo.write("app.py", APP_V2 + "# resolved differently\n")
    after = repo.commit("resolve conflict")

    shouldRun, _ = repo.decide(
        baseSha=baseSha, headSha=after, before=before, after=after
    )

    assert shouldRun is True


def rebased_scenario(repo):
    """PR branch with one code commit, rebased onto a develop that gained a commit."""
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("app.py", APP_V2)
    before = repo.commit("pr change")
    repo.git("checkout", "-q", "develop")
    repo.write("other.py", OTHER)
    baseSha = repo.commit("develop moves")
    repo.git("checkout", "-q", "feature")
    repo.git("rebase", "-q", "develop")
    after = repo.sha()
    return {"baseSha": baseSha, "headSha": after, "before": before, "after": after}


def test_skips_after_rebase_onto_new_base_with_identical_pr_code(repo):
    scenario = rebased_scenario(repo)

    shouldRun, reason = repo.decide(**scenario)

    assert not repo.is_ancestor(scenario["before"], scenario["after"])
    assert shouldRun is False
    assert "only the base moved" in reason


@pytest.mark.parametrize("before", [ZEROS, ""])
def test_runs_when_before_is_empty_or_all_zeros(repo, before):
    scenario = base_moved_scenario(repo)
    scenario["before"] = before

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_before_is_missing_locally_and_fetch_fails(repo):
    scenario = base_moved_scenario(repo)
    scenario["before"] = "a" * 40

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_skips_when_missing_before_can_be_fetched_from_origin(repo, tmp_path):
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--no-local", str(repo.path), str(clone)],
        check=True,
        capture_output=True,
    )
    repo.git("config", "uploadpack.allowAnySHA1InWant", "true")
    scenario = rebased_scenario(repo)
    git = partial(ci_change_scope.run_git, cwd=clone)
    subprocess.run(
        ["git", "fetch", "-q", "origin", "develop", "feature"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    assert git(["cat-file", "-e", scenario["before"] + "^{commit}"]).returncode != 0

    shouldRun, _ = ci_change_scope.decide_scope(
        git=git,
        event="pull_request",
        action="synchronize",
        priorConclusion="success",
        **scenario,
    )

    assert shouldRun is False


def test_skips_pull_request_whose_whole_diff_is_changes_md(repo):
    repo.git("checkout", "-q", "-b", "feature")
    base = repo.sha()
    repo.write("CHANGES.md", "# changes\n- entry\n")
    head = repo.commit("log change")

    shouldRun, reason = repo.decide(
        action="opened", baseSha=base, headSha=head, priorConclusion=""
    )

    assert shouldRun is False
    assert "CHANGES.md" in reason


def test_runs_when_pull_request_changes_code_and_changes_md(repo):
    repo.git("checkout", "-q", "-b", "feature")
    base = repo.sha()
    repo.write("CHANGES.md", "# changes\n- entry\n")
    repo.write("app.py", APP_V2)
    head = repo.commit("both")

    shouldRun, _ = repo.decide(action="opened", baseSha=base, headSha=head)

    assert shouldRun is True


def test_skips_push_whose_range_is_changes_md_only(repo):
    before = repo.sha()
    repo.write("CHANGES.md", "# changes\n- entry\n")
    after = repo.commit("log change")

    shouldRun, _ = repo.decide(event="push", action="", before=before, after=after)

    assert shouldRun is False


def test_runs_when_push_has_all_zero_before(repo):
    repo.write("CHANGES.md", "# changes\n- entry\n")
    after = repo.commit("log change")

    shouldRun, _ = repo.decide(event="push", action="", before=ZEROS, after=after)

    assert shouldRun is True


def test_runs_when_push_range_is_not_a_valid_diff(repo):
    shouldRun, _ = repo.decide(
        event="push", action="", before="b" * 40, after=repo.sha()
    )

    assert shouldRun is True


def test_runs_when_pull_request_shas_are_missing(repo):
    shouldRun, _ = repo.decide(action="opened", baseSha="", headSha="")

    assert shouldRun is True


def test_runs_when_opened_pull_request_has_code_changes(repo):
    repo.git("checkout", "-q", "-b", "feature")
    base = repo.sha()
    repo.write("app.py", APP_V2)
    head = repo.commit("code")

    shouldRun, _ = repo.decide(
        action="opened", before=head, after=head, baseSha=base, headSha=head
    )

    assert shouldRun is True


def test_runs_when_synchronize_leaves_no_code_diff_against_base(repo):
    scenario = base_moved_scenario(repo)
    repo.write("app.py", APP_V1)
    after = repo.commit("revert pr code")
    scenario.update(headSha=after, after=after)

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_git_fails_computing_fingerprint(repo):
    scenario = base_moved_scenario(repo)
    scenario["baseSha"] = "c" * 40

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_main_appends_exactly_one_output_line_and_prints_reason(
    repo, tmp_path, monkeypatch, capsys
):
    scenario = base_moved_scenario(repo)
    outputFile = tmp_path / "github_output"
    outputFile.write_text("existing=1\n")
    monkeypatch.chdir(repo.path)

    exitCode = ci_change_scope.main(
        [
            "--event",
            "pull_request",
            "--action",
            "synchronize",
            "--base-sha",
            scenario["baseSha"],
            "--head-sha",
            scenario["headSha"],
            "--before",
            scenario["before"],
            "--after",
            scenario["after"],
            "--prior-conclusion",
            "success",
            "--output",
            str(outputFile),
        ]
    )

    assert exitCode == 0
    assert outputFile.read_text() == "existing=1\ncode=false\n"
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_main_writes_code_true_when_scope_needs_the_suite(repo, tmp_path, monkeypatch):
    outputFile = tmp_path / "github_output"
    monkeypatch.chdir(repo.path)

    ci_change_scope.main(["--event", "push", "--output", str(outputFile)])

    assert outputFile.read_text() == "code=true\n"


def test_main_rejects_unknown_event(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        ci_change_scope.main(["--event", "schedule", "--output", str(tmp_path / "out")])

    assert excinfo.value.code == 2


def test_runs_when_synchronize_has_no_base_sha(repo):
    scenario = base_moved_scenario(repo)
    scenario["baseSha"] = ""

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_before_shares_no_history_with_the_base(repo):
    scenario = base_moved_scenario(repo)
    repo.git("checkout", "-q", "--orphan", "unrelated")
    repo.write("app.py", APP_V2)
    scenario["before"] = repo.commit("unrelated root")

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_skips_pull_request_when_only_the_base_gained_code_after_the_fork(repo):
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("CHANGES.md", "# changes\n- entry\n")
    head = repo.commit("log change")
    repo.git("checkout", "-q", "develop")
    repo.write("other.py", OTHER)
    base = repo.commit("develop gains code after the fork")

    shouldRun, reason = repo.decide(
        action="opened", baseSha=base, headSha=head, priorConclusion=""
    )

    assert shouldRun is False
    assert "CHANGES.md" in reason


def same_base_pushes(repo, beforeFiles, afterFiles):
    """Two consecutive PR commits on one base: before holds beforeFiles, after afterFiles."""
    repo.git("checkout", "-q", "-b", "feature")
    base = repo.sha()
    for name, content in beforeFiles.items():
        repo.write(name, content)
    before = repo.commit("before push")
    for name, content in afterFiles.items():
        repo.write(name, content)
    after = repo.commit("after push")
    return {"baseSha": base, "headSha": after, "before": before, "after": after}


def test_runs_when_binary_file_content_differs_between_pushes(repo):
    scenario = same_base_pushes(
        repo, {"blob.bin": b"\x00\x01A"}, {"blob.bin": b"\x00\x01B"}
    )

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_the_same_added_line_moves_to_another_position(repo):
    atTop = "def app():\n    extra = 1\n    value = 1\n    return value\n"
    atBottom = "def app():\n    value = 1\n    return value\n    extra = 1\n"
    scenario = same_base_pushes(repo, {"app.py": atTop}, {"app.py": atBottom})

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_only_line_endings_change_to_crlf(repo):
    scenario = same_base_pushes(
        repo,
        {"app.py": APP_V2.encode()},
        {"app.py": APP_V2.replace("\n", "\r\n").encode()},
    )

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_a_code_file_is_renamed_to_changes_md(repo):
    repo.git("config", "diff.renames", "true")
    repo.git("rm", "-q", "CHANGES.md")
    base = repo.commit("drop changes file")
    repo.git("checkout", "-q", "-b", "feature")
    repo.git("mv", "app.py", "CHANGES.md")
    head = repo.commit("rename code to changes")

    shouldRun, _ = repo.decide(action="opened", baseSha=base, headSha=head)

    assert shouldRun is True


def test_does_not_crash_on_non_utf8_file_content(repo):
    scenario = same_base_pushes(
        repo, {"latin.py": b"caf\xe9 = 1\n"}, {"CHANGES.md": "# changes\n- entry\n"}
    )

    shouldRun, reason = repo.decide(**scenario)

    assert shouldRun is False
    assert "only the base moved" in reason


def test_runs_when_a_changed_file_name_is_not_utf8():
    def stubGit(args):
        return subprocess.CompletedProcess(args, 0, stdout=b"caf\xe9.py\0", stderr=b"")

    shouldRun, _ = ci_change_scope.decide_scope(
        git=stubGit,
        event="push",
        action="",
        baseSha="",
        headSha="",
        before="a" * 40,
        after="b" * 40,
        priorConclusion="",
    )

    assert shouldRun is True


@pytest.mark.parametrize(
    "mangle",
    [
        lambda sha: sha[:12],
        lambda sha: sha.upper(),
        lambda sha: sha + "0",
        lambda sha: "g" * 40,
    ],
    ids=["abbreviated", "uppercase", "too_long", "not_hex"],
)
@pytest.mark.parametrize("field", ["before", "after", "baseSha"])
def test_runs_when_a_sha_argument_is_not_40_lowercase_hex(repo, field, mangle):
    scenario = base_moved_scenario(repo)
    scenario[field] = mangle(scenario[field])

    shouldRun, _ = repo.decide(**scenario)

    assert shouldRun is True


def test_runs_when_changes_md_only_range_uses_abbreviated_shas(repo):
    repo.git("checkout", "-q", "-b", "feature")
    base = repo.sha()
    repo.write("CHANGES.md", "# changes\n- entry\n")
    head = repo.commit("log change")

    shouldRun, _ = repo.decide(action="opened", baseSha=base[:12], headSha=head)

    assert shouldRun is True


def test_main_writes_code_true_when_git_raises_os_error(tmp_path, capsys):
    def brokenGit(args):
        raise OSError("git is not installed")

    outputFile = tmp_path / "github_output"

    exitCode = ci_change_scope.main(
        [
            "--event",
            "push",
            "--before",
            "a" * 40,
            "--after",
            "b" * 40,
            "--output",
            str(outputFile),
        ],
        git=brokenGit,
    )

    captured = capsys.readouterr()
    assert exitCode == 0
    assert outputFile.read_text() == "code=true\n"
    assert "decision failed; running the test suite" in captured.out
    assert "git is not installed" in captured.err


def test_run_git_reports_failure_when_git_cannot_start(tmp_path):
    result = ci_change_scope.run_git(["status"], cwd=tmp_path / "missing")

    assert result.returncode != 0


def test_run_git_reports_failure_when_git_times_out(monkeypatch):
    def timingOut(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(ci_change_scope.subprocess, "run", timingOut)

    result = ci_change_scope.run_git(["status"])

    assert result.returncode != 0


def test_run_git_sets_timeout_and_disables_terminal_prompts(monkeypatch):
    seen = {}

    def capturing(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(ci_change_scope.subprocess, "run", capturing)

    ci_change_scope.run_git(["status"])

    assert seen["timeout"] == 120
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "text" not in seen


def test_main_rejects_abbreviated_option_names(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        ci_change_scope.main(["--event", "push", "--out", str(tmp_path / "out")])

    assert excinfo.value.code == 2
