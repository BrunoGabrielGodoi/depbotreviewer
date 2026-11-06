import subprocess
from pathlib import Path

import pytest

from dep_reviewer.repo_tools import PAGE_LINES, RepoTools, ToolError, page_lines


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> RepoTools:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")  # throwaway fixture repo; ignore global signing setup
    (root / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
    (root / "src").mkdir()
    (root / "src" / "index.js").write_text("const pad = require('left-pad');\nconsole.log(pad('x', 3));\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "package.json").write_text('{"dependencies": {"left-pad": "2.0.0"}}\n')
    git(root, "commit", "-q", "-am", "bump")
    head = git(root, "rev-parse", "HEAD")
    (tmp_path / "secret.txt").write_text("outside")
    return RepoTools(root, base, head)


def test_search_finds_usage(repo: RepoTools):
    out = repo.execute("search_code", {"pattern": "require\\('left-pad'\\)"})
    assert "src/index.js:1:" in out
    assert "[1 matches]" in out


def test_search_no_match(repo: RepoTools):
    assert repo.execute("search_code", {"pattern": "does-not-exist"}) == "No matches."


def test_search_with_glob(repo: RepoTools):
    out = repo.execute("search_code", {"pattern": "left-pad", "path_glob": "src/**"})
    assert "src/index.js" in out and "package.json" not in out


def test_read_file_numbers_lines(repo: RepoTools):
    out = repo.execute("read_file", {"path": "src/index.js"})
    assert out.startswith("1: const pad")
    assert "lines 1-2 of 2" in out


@pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", "src/../../secret.txt", ".git/config"])
def test_read_file_rejects_escape(repo: RepoTools, path: str):
    with pytest.raises(ToolError):
        repo.execute("read_file", {"path": path})


def test_show_pr_diff(repo: RepoTools):
    out = repo.execute("show_pr_diff", {"path": "package.json"})
    assert '+{"dependencies": {"left-pad": "2.0.0"}}' in out


def test_list_files_glob(repo: RepoTools):
    out = repo.execute("list_files", {"glob": "**/*.js"})
    assert "src/index.js" in out and "package.json" not in out


def test_test_log_unavailable(repo: RepoTools):
    with pytest.raises(ToolError):
        repo.execute("read_test_log", {})


def test_rejects_bad_input(repo: RepoTools):
    with pytest.raises(ToolError):
        repo.execute("read_file", {"path": 3})
    with pytest.raises(ToolError):
        repo.execute("read_file", {"path": "src/index.js", "start_line": "2"})
    with pytest.raises(ToolError):
        repo.execute("nope", {})


def test_page_lines_paging():
    lines = [f"l{i}" for i in range(1, PAGE_LINES + 51)]
    first = page_lines(lines, 1, "f", numbered=False)
    assert f"start_line={PAGE_LINES + 1}" in first
    second = page_lines(lines, PAGE_LINES + 1, "f", numbered=False)
    assert second.endswith(f"lines {PAGE_LINES + 1}-{PAGE_LINES + 50} of {PAGE_LINES + 50}]")
