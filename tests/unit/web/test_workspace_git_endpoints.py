from __future__ import annotations

import subprocess
from pathlib import Path

from tests.unit.web.test_workspace_context_endpoint import CSRF_HEADERS, FakeTM, _login_client
from web.thread_metadata import ThreadMetadata


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _commit(repo: Path, message: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_git_workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _commit(repo, "init")
    return repo


def _make_tm(workspace_root: Path) -> FakeTM:
    return FakeTM(
        [
            ThreadMetadata(
                id="thread-000000000010",
                name="Claude",
                preset_id="",
                backend_kind="claude_code",
                sdk_session_id="sdk-1",
                cwd=str(workspace_root),
                created_at=1.0,
                updated_at=2.0,
                message_count=0,
            )
        ]
    )


def test_workspace_git_status_and_diff(tmp_path: Path) -> None:
    repo = _make_git_workspace(tmp_path)
    (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    (repo / "notes.txt").write_text("draft\n", encoding="utf-8")

    client = _login_client(tmp_path, _make_tm(repo))
    try:
        status_resp = client.get("/api/threads/thread-000000000010/workspace-git/status")
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["current_branch"] == "main"
        paths = {item["path"]: item for item in body["changes"]}
        assert "README.md" in paths
        assert "notes.txt" in paths
        assert paths["notes.txt"]["staged_status"] == "?"
        assert paths["notes.txt"]["unstaged_status"] == "?"

        diff_resp = client.get(
            "/api/threads/thread-000000000010/workspace-git/file-diff",
            params={"path": "README.md"},
        )
        assert diff_resp.status_code == 200
        assert "world" in diff_resp.json()["diff"]

        untracked_resp = client.get(
            "/api/threads/thread-000000000010/workspace-git/file-diff",
            params={"path": "notes.txt"},
        )
        assert untracked_resp.status_code == 200
        assert "draft" in untracked_resp.json()["diff"]
    finally:
        client.__exit__(None, None, None)


def test_workspace_git_branches_and_commits(tmp_path: Path) -> None:
    repo = _make_git_workspace(tmp_path)
    _run_git(repo, "checkout", "-b", "feature/demo")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _run_git(repo, "add", "feature.txt")
    _commit(repo, "add feature")

    client = _login_client(tmp_path, _make_tm(repo))
    try:
        branches_resp = client.get("/api/threads/thread-000000000010/workspace-git/branches")
        assert branches_resp.status_code == 200
        branches = branches_resp.json()
        assert branches["current_branch"] == "feature/demo"
        assert "main" in branches["local_branches"]
        assert "feature/demo" in branches["local_branches"]

        commits_resp = client.get("/api/threads/thread-000000000010/workspace-git/commits")
        assert commits_resp.status_code == 200
        commits = commits_resp.json()["commits"]
        assert commits[0]["subject"] == "add feature"
        assert commits[1]["subject"] == "init"
    finally:
        client.__exit__(None, None, None)


def test_workspace_git_rejects_non_repo(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    client = _login_client(tmp_path, _make_tm(workspace))
    try:
        resp = client.get("/api/threads/thread-000000000010/workspace-git/status")
        assert resp.status_code == 400
        assert "git" in resp.json()["detail"].lower()
    finally:
        client.__exit__(None, None, None)


def test_workspace_git_stage_unstage_and_commit(tmp_path: Path) -> None:
    repo = _make_git_workspace(tmp_path)
    (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    (repo / "notes.txt").write_text("draft\n", encoding="utf-8")

    client = _login_client(tmp_path, _make_tm(repo))
    try:
        stage_resp = client.post(
            "/api/threads/thread-000000000010/workspace-git/stage",
            json={"paths": ["README.md", "notes.txt"]},
            headers=CSRF_HEADERS,
        )
        assert stage_resp.status_code == 200
        status_after_stage = _run_git(repo, "status", "--porcelain=1")
        assert "M  README.md" in status_after_stage
        assert "A  notes.txt" in status_after_stage

        unstage_resp = client.post(
            "/api/threads/thread-000000000010/workspace-git/unstage",
            json={"paths": ["notes.txt"]},
            headers=CSRF_HEADERS,
        )
        assert unstage_resp.status_code == 200
        status_after_unstage = _run_git(repo, "status", "--porcelain=1")
        assert "M  README.md" in status_after_unstage
        assert "?? notes.txt" in status_after_unstage

        commit_resp = client.post(
            "/api/threads/thread-000000000010/workspace-git/commit",
            json={"message": "update readme"},
            headers=CSRF_HEADERS,
        )
        assert commit_resp.status_code == 200
        body = commit_resp.json()
        assert body["short_commit"]
        assert _run_git(repo, "log", "-1", "--pretty=%s").strip() == "update readme"
        assert "?? notes.txt" in _run_git(repo, "status", "--porcelain=1")
    finally:
        client.__exit__(None, None, None)


def test_workspace_git_create_branch_and_checkout(tmp_path: Path) -> None:
    repo = _make_git_workspace(tmp_path)

    client = _login_client(tmp_path, _make_tm(repo))
    try:
        create_resp = client.post(
            "/api/threads/thread-000000000010/workspace-git/create-branch",
            json={"branch": "feature/web-git-v2", "checkout": True},
            headers=CSRF_HEADERS,
        )
        assert create_resp.status_code == 200
        assert create_resp.json()["current_branch"] == "feature/web-git-v2"
        assert _run_git(repo, "branch", "--show-current").strip() == "feature/web-git-v2"

        checkout_resp = client.post(
            "/api/threads/thread-000000000010/workspace-git/checkout",
            json={"branch": "main"},
            headers=CSRF_HEADERS,
        )
        assert checkout_resp.status_code == 200
        assert checkout_resp.json()["current_branch"] == "main"
        assert _run_git(repo, "branch", "--show-current").strip() == "main"
    finally:
        client.__exit__(None, None, None)
