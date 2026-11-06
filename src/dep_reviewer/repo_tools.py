"""Read-only tools Claude uses to explore the checked-out repository.

Every tool is read-only and confined to the repository root. Large outputs are
paged, never silently cut: each page says which lines it holds and how to get the next.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PAGE_LINES = 400
MAX_SEARCH_RESULTS = 200
MAX_LIST_RESULTS = 500
MAX_LINE_CHARS = 400


class ToolError(Exception):
    """An error reported back to Claude as a failed tool result."""


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_code",
        "description": (
            "Search tracked files in the repository (at the PR's head commit) with a regular "
            "expression, using `git grep -E`. Use this to find imports, call sites, config keys "
            "and other usages of the updated dependency. Returns `path:line: text` matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Extended regular expression (POSIX ERE)."},
                "path_glob": {
                    "type": "string",
                    "description": "Optional glob that limits which files are searched, e.g. 'src/**/*.ts'.",
                },
                "ignore_case": {"type": "boolean", "description": "Case-insensitive match. Default false."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            f"Read a file from the repository at the PR's head commit, {PAGE_LINES} numbered lines "
            "per call. Pass start_line to read further."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repository root."},
                "start_line": {"type": "integer", "description": "1-based line to start from. Default 1."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_files",
        "description": "List tracked files in the repository, optionally filtered by a glob such as '**/package.json'.",
        "input_schema": {
            "type": "object",
            "properties": {"glob": {"type": "string", "description": "Optional glob filter."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "show_pr_diff",
        "description": (
            f"Show this PR's diff for one file (base...head), {PAGE_LINES} lines per call. Use it for "
            "lockfiles and any file whose diff was not included inline."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repository root."},
                "start_line": {"type": "integer", "description": "1-based diff line to start from. Default 1."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_test_log",
        "description": f"Read the full build/test log, {PAGE_LINES} lines per call. Only available when tests were run.",
        "input_schema": {
            "type": "object",
            "properties": {"start_line": {"type": "integer", "description": "1-based line to start from. Default 1."}},
            "additionalProperties": False,
        },
    },
]


def page_lines(lines: list[str], start_line: int, label: str, numbered: bool) -> str:
    total = len(lines)
    if total == 0:
        return f"[{label}: empty]"
    start = max(1, start_line)
    if start > total:
        return f"[{label}: start_line {start} is past the end ({total} lines)]"
    end = min(total, start + PAGE_LINES - 1)
    body = "\n".join(
        (f"{i}: {lines[i - 1]}" if numbered else lines[i - 1]) for i in range(start, end + 1)
    )
    more = f"; call again with start_line={end + 1} for more" if end < total else ""
    return f"{body}\n[{label}: lines {start}-{end} of {total}{more}]"


def _clip(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    return f"{line[:MAX_LINE_CHARS]}… [line is {len(line)} chars; shortened]"


@dataclass
class RepoTools:
    root: Path
    base_sha: str
    head_sha: str
    test_log: Path | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    # --- git helpers -------------------------------------------------------

    def _git(self, *args: str, ok_codes: tuple[int, ...] = (0,)) -> str:
        r = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode not in ok_codes:
            raise ToolError(r.stderr.strip() or f"git {args[0]} exited with {r.returncode}")
        return r.stdout

    def ensure_commits(self) -> None:
        """Fetch the PR's base and head commits if the checkout does not have them."""
        for sha in (self.base_sha, self.head_sha):
            present = subprocess.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=self.root, capture_output=True
            ).returncode == 0
            if not present:
                self._git("fetch", "--no-tags", "--quiet", "origin", sha)

    def resolve(self, rel: str) -> Path:
        if not rel or "\x00" in rel:
            raise ToolError("path is empty or invalid")
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError("path is outside the repository")
        if ".git" in target.relative_to(self.root).parts:
            raise ToolError("the .git directory is not readable")
        return target

    # --- tools -------------------------------------------------------------

    def search_code(self, pattern: str, path_glob: str | None = None, ignore_case: bool = False) -> str:
        args = ["grep", "-n", "-I", "-E", "--full-name"]
        if ignore_case:
            args.append("-i")
        args += ["-e", pattern]
        if path_glob:
            args += ["--", f":(glob){path_glob}"]
        out = self._git(*args, ok_codes=(0, 1))  # 1 = no matches
        matches = out.splitlines()
        if not matches:
            return "No matches."
        shown = [_clip(m) for m in matches[:MAX_SEARCH_RESULTS]]
        footer = (
            f"\n[{len(matches)} matches; showing the first {MAX_SEARCH_RESULTS}. Narrow the pattern or path_glob to see the rest.]"
            if len(matches) > MAX_SEARCH_RESULTS
            else f"\n[{len(matches)} matches]"
        )
        return "\n".join(shown) + footer

    def read_file(self, path: str, start_line: int = 1) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"{path} is not a file")
        raw = target.read_bytes()
        if b"\x00" in raw[:8192]:
            return f"[{path} is a binary file ({len(raw)} bytes)]"
        lines = [_clip(l) for l in raw.decode("utf-8", errors="replace").splitlines()]
        return page_lines(lines, start_line, path, numbered=True)

    def list_files(self, glob: str | None = None) -> str:
        files = self._git("ls-files").splitlines()
        if glob:
            files = [f for f in files if fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(f, glob.removeprefix("**/"))]
        if not files:
            return "No files."
        if len(files) > MAX_LIST_RESULTS:
            return "\n".join(files[:MAX_LIST_RESULTS]) + (
                f"\n[{len(files)} files; showing the first {MAX_LIST_RESULTS}. Use a narrower glob to see the rest.]"
            )
        return "\n".join(files) + f"\n[{len(files)} files]"

    def show_pr_diff(self, path: str, start_line: int = 1) -> str:
        self.resolve(path)  # validates the path; the file may be deleted at head
        out = self._git("diff", f"{self.base_sha}...{self.head_sha}", "--", path)
        if not out:
            return f"[no changes to {path} in this PR]"
        return page_lines([_clip(l) for l in out.splitlines()], start_line, f"diff of {path}", numbered=False)

    def read_test_log(self, start_line: int = 1) -> str:
        if self.test_log is None or not self.test_log.exists():
            raise ToolError("no tests were run for this review")
        lines = [_clip(l) for l in self.test_log.read_text(encoding="utf-8", errors="replace").splitlines()]
        return page_lines(lines, start_line, "test log", numbered=True)

    # --- dispatch ----------------------------------------------------------

    def execute(self, name: str, args: Any) -> str:
        """Validate the input and run the tool. Raises ToolError for bad input or failures."""
        if not isinstance(args, dict):
            raise ToolError("tool input must be an object")

        def s(key: str, required: bool = False) -> str | None:
            v = args.get(key)
            if v is None:
                if required:
                    raise ToolError(f"'{key}' is required")
                return None
            if not isinstance(v, str):
                raise ToolError(f"'{key}' must be a string")
            return v

        def n(key: str) -> int:
            v = args.get(key, 1)
            if not isinstance(v, int) or isinstance(v, bool):
                raise ToolError(f"'{key}' must be an integer")
            return v

        if name == "search_code":
            ic = args.get("ignore_case", False)
            if not isinstance(ic, bool):
                raise ToolError("'ignore_case' must be a boolean")
            return self.search_code(s("pattern", required=True), s("path_glob"), ic)
        if name == "read_file":
            return self.read_file(s("path", required=True), n("start_line"))
        if name == "list_files":
            return self.list_files(s("glob"))
        if name == "show_pr_diff":
            return self.show_pr_diff(s("path", required=True), n("start_line"))
        if name == "read_test_log":
            return self.read_test_log(n("start_line"))
        raise ToolError(f"unknown tool {name!r}")
