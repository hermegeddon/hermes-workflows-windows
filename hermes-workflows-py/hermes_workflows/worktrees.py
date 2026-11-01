"""Checked local worktrees and replayable, fast-forward-only stack barriers.

Hermes supplies its hardened, noninteractive Git runner. This module never
resets, removes or pushes a branch or worktree; native workspace cleanup remains
responsible for its own dirty/unpushed-work and Windows file-lock checks.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path


class WorktreeError(ValueError):
    pass


@contextmanager
def repository_lock(common):
    # OS locks disappear on process death. Keep the file: unlinking it while
    # another process has it open would create two independent lock identities.
    with (Path(common) / "hermes-workflows.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorktreeError(
                "Another workflow is updating this repository; retry later"
            ) from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Worktrees:
    def __init__(self, host):
        self.host = host

    def git(self, repo, *args, check=True):
        if any(
            os.environ.get(k)
            for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")
        ):
            raise WorktreeError("Clear inherited Git repository overrides before running worktrees")
        try:
            result = self.host.call("workspace_git", "_git", Path(repo), *args, timeout=10)
        except Exception as exc:
            raise WorktreeError(
                f"Git operation failed in {repo} ({type(exc).__name__}); retry after resolving the repository or file lock"
            ) from exc
        if check and result.returncode:
            # Git stderr can contain user repo configuration. Keep the error
            # actionable without copying arbitrary process output into the UI.
            raise WorktreeError(
                f"Git {args[0]} failed in {repo}; inspect its branch, working tree and file locks, then retry"
            )
        return result

    def output(self, repo, *args):
        return self.git(repo, *args).stdout.rstrip("\r\n")

    def sha(self, repo, ref):
        value = self.output(repo, "rev-parse", "--verify", ref + "^{commit}")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            raise WorktreeError("Git returned an invalid commit identity")
        return value

    def branch(self, repo):
        return self.output(repo, "symbolic-ref", "--quiet", "--short", "HEAD")

    def common(self, repo):
        return str(
            Path(
                self.output(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).resolve()
        )

    def registrations(self, repo):
        records = self.output(repo, "worktree", "list", "--porcelain", "-z")
        return [
            dict(field.split(" ", 1) for field in block.split("\0") if " " in field)
            for block in records.split("\0\0")
            if block
        ]

    def clean(self, repo, *, allow_worktrees=False):
        repo = Path(repo).resolve()
        git_dir = Path(self.output(repo, "rev-parse", "--path-format=absolute", "--git-dir"))
        for name in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
        ):
            if (git_dir / name).exists():
                raise WorktreeError(f"Finish the existing Git operation in {repo}, then retry")
        permitted = []
        if allow_worktrees:
            for record in self.registrations(repo):
                path = Path(record["worktree"]).resolve()
                if path != repo and path.is_relative_to(repo):
                    permitted.append(path.relative_to(repo).as_posix().rstrip("/") + "/")
        changes = self.output(
            repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        ).split("\0")
        for change in changes:
            if not change:
                continue
            if change.startswith("?? ") and any(
                change[3:].replace("\\", "/").startswith(p) for p in permitted
            ):
                continue
            raise WorktreeError(f"Uncommitted changes in {repo}; commit or stash them, then retry")

    def context(self, node, native, board):
        configured = node.get("workdir") or native.read_board_metadata(board).get("default_workdir")
        if not configured:
            raise WorktreeError(
                "Set the node workdir or board default_workdir to an absolute project repository"
            )
        repo = Path(configured).expanduser()
        if not repo.is_absolute():
            raise WorktreeError("Worktree repository paths must be absolute")
        repo = Path(self.output(repo, "rev-parse", "--show-toplevel")).resolve()
        try:
            module = self.host.loader("hermes_cli")
            hermes = Path(module.__file__).resolve().parent.parent
        except (ImportError, AttributeError, TypeError):
            hermes = None
        if hermes and repo.is_relative_to(hermes):
            raise WorktreeError(
                "Worktrees must be anchored in the project repository, outside the Hermes code checkout"
            )
        branch = node.get("branch") or self.branch(repo)
        if not isinstance(branch, str) or branch.startswith("-") or "{{" in branch:
            raise WorktreeError("The shared branch must resolve to a local Git branch")
        self.git(repo, "check-ref-format", "refs/heads/" + branch)
        context = {
            "repo": str(repo),
            "common": self.common(repo),
            "branch": branch,
            "tip": self.sha(repo, "refs/heads/" + branch),
        }
        with repository_lock(context["common"]):
            self.check(context)
        return context

    def check(self, context, *, tips=None):
        repo = context["repo"]
        if self.common(repo) != context["common"]:
            raise WorktreeError(
                "Repository identity changed; restore the original repository before retrying"
            )
        if self.branch(repo) != context["branch"]:
            raise WorktreeError(
                f"Check out the expected shared branch {context['branch']} in {repo}, then retry"
            )
        tip = self.sha(repo, "HEAD")
        if tip not in (tips or [context["tip"]]):
            raise WorktreeError(
                "The shared branch moved outside this workflow; reconcile its commits before retrying"
            )
        self.clean(repo, allow_worktrees=True)
        return tip

    @staticmethod
    def target(context, task_id):
        if not re.fullmatch(r"t_[a-z0-9]+", task_id):
            raise WorktreeError("Invalid native card identity for a worktree")
        repo = Path(context["repo"])
        target = repo / ".worktrees" / task_id
        if target.resolve() != target or target.parent.resolve() != target.parent:
            raise WorktreeError(
                "The managed worktree path resolves through a link; choose an unlinked project path"
            )
        return {"path": str(target), "branch": "wf/" + task_id, "base": context["tip"]}

    def materialize(self, context, workspace):
        repo, target = context["repo"], Path(workspace["path"])
        with repository_lock(context["common"]):
            self.check(context)
            expected = self.target(context, target.name)
            if workspace["path"] != expected["path"] or workspace["branch"] != expected["branch"]:
                raise WorktreeError(
                    "Worktree path or branch is outside this card's managed workspace"
                )
            # Recheck after recovery: never repurpose an existing checkout or
            # silently accept the dispatcher's different-branch fallback.
            registrations = {
                str(Path(r["worktree"]).resolve()): r for r in self.registrations(repo)
            }
            registered = registrations.get(str(target))
            if target.exists() and not registered:
                raise WorktreeError(
                    f"Worktree path is occupied: {target}; preserve or relocate it before retrying"
                )
            exists = (
                self.git(
                    repo,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    "refs/heads/" + workspace["branch"],
                    check=False,
                ).returncode
                == 0
            )
            if exists and self.sha(repo, "refs/heads/" + workspace["branch"]) != workspace["base"]:
                raise WorktreeError(
                    "The card branch already contains different commits; reconcile it before retrying"
                )
            if registered:
                self.verify_workspace(context, workspace, expected_tip=workspace["base"])
                return
            if not exists:
                self.git(repo, "branch", "--no-track", workspace["branch"], workspace["base"])
            target.parent.mkdir(parents=True, exist_ok=True)
            self.git(repo, "worktree", "add", str(target), workspace["branch"])
            self.verify_workspace(context, workspace, expected_tip=workspace["base"])

    def for_adoption(self, context, identity, *, terminal, review):
        workspace = self.target(context, identity)
        if terminal:
            with repository_lock(context["common"]):
                self.check(context)
                exists = (
                    self.git(
                        context["repo"],
                        "show-ref",
                        "--verify",
                        "--quiet",
                        "refs/heads/" + workspace["branch"],
                        check=False,
                    ).returncode
                    == 0
                )
                if exists:
                    self.verify_workspace(context, workspace)
                    return workspace
            if not review:
                return None
        self.materialize(context, workspace)
        return workspace

    def verify_workspace(self, context, workspace, *, expected_tip=None):
        path = Path(workspace["path"])
        if (
            path.resolve() != path
            or self.common(path) != context["common"]
            or self.branch(path) != workspace["branch"]
        ):
            raise WorktreeError(
                "Card worktree path, repository or branch changed; reconcile it before retrying"
            )
        self.clean(path)
        tip = self.sha(path, "HEAD")
        if expected_tip is not None and tip != expected_tip:
            raise WorktreeError("Card worktree commit changed; reconcile it before retrying")
        return tip

    def barrier_intent(self, context, workspace):
        with repository_lock(context["common"]):
            before = self.check(context)
            card_tip = after = self.verify_workspace(context, workspace)
            if self.git(
                context["repo"], "merge-base", "--is-ancestor", before, after, check=False
            ).returncode:
                if self.git(
                    context["repo"], "merge-base", "--is-ancestor", after, before, check=False
                ).returncode:
                    raise WorktreeError(
                        "Card commits cannot fast-forward the shared branch; reconcile the divergent branches"
                    )
                after = before
            return {
                "before": before,
                "after": after,
                "card_tip": card_tip,
                "workspace": workspace,
                "status": "prepared",
            }

    def advance(self, context, intent):
        with repository_lock(context["common"]):
            current = self.check(context, tips=[intent["before"], intent["after"]])
            self.verify_workspace(
                context, intent["workspace"], expected_tip=intent.get("card_tip", intent["after"])
            )
            if current != intent["after"]:
                self.git(context["repo"], "merge", "--ff-only", "--no-edit", intent["after"])
            self.check(context, tips=[intent["after"]])
        return intent["after"]


def release_directive(branch):
    return (
        "\n\n[hermes-workflows: release stacking]\n"
        f"Build on the existing commits of shared branch `{branch}`. Commit on this card's "
        "current branch before completing it. Do not switch branches or bump versions/CHANGELOG; "
        "the dedicated release step handles those once for the whole scope.\n"
    )
