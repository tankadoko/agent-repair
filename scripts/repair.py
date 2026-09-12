"""Local session evidence and reversible file updates; reasoning belongs to the skill."""
from contextlib import ExitStack, contextmanager
import argparse
import codecs
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

REPO = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(path, value):
    write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def plain_path(path):
    path = path.absolute()
    for part in (path, *path.parents):
        require(not part.is_symlink() and not (
            part.exists() and getattr(part.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)), "Links are not repair targets")
    return path.resolve()


def private(path):
    path = plain_path(path.expanduser())
    require(not path.is_relative_to(REPO), "Keep case data outside this repository")
    return path


@contextmanager
def lock(path):
    directory = Path(tempfile.gettempdir()) / "agent-repair-locks"
    directory.mkdir(exist_ok=True)
    key = hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest()
    with (directory / key).open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def inspect_session(value, home, state):
    source = Path(value).expanduser()
    if not source.is_file():
        ids = set(re.findall(r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", value.lower()))
        require(len(ids) == 1, "Supply one session UUID or a local JSONL file")
        matches = {p.resolve() for folder in ("sessions", "archived_sessions")
                   for p in (home / folder).rglob(f"*{next(iter(ids))}*.jsonl")}
        require(len(matches) == 1, "Session not found uniquely; supply its JSONL path")
        source = matches.pop()
    source = source.resolve()
    metadata, events, fallback, gaps = {}, [], [], []
    with source.open(encoding="utf-8-sig") as stream:
        for line, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
                require(isinstance(record, dict), "Expected a record")
                kind, payload = record.get("type"), record.get("payload", {})
                require(isinstance(payload, dict), "Expected a payload")
            except ValueError:
                gaps.append({"line": line, "reason": "malformed_record"})
                continue
            if kind in {"session_meta", "turn_context"}:
                metadata.update({k: payload[k] for k in ("id", "cwd", "automation_id") if k in payload})
            elif kind in {"compacted", "context_compacted"}:
                gaps.append({"line": line, "reason": "compaction"})
            elif kind == "response_item":
                typ = payload.get("type")
                if typ == "message" and payload.get("role") in {"user", "assistant"}:
                    content = payload.get("content", [])
                    body = content if isinstance(content, str) else "\n".join(
                        item.get("text", "[non-text attachment omitted]") for item in content if isinstance(item, dict))
                    events.append({"line": line, "kind": payload["role"], "text": body})
                elif typ in {"function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"}:
                    events.append({"line": line, "kind": typ, **{k: payload[k] for k in
                                   ("name", "call_id", "arguments", "input", "output") if k in payload}})
            elif kind == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
                fallback.append({"line": line, "kind": payload["type"], "text": payload.get("message", "")})
    require(events or fallback, "No supported conversation events found")
    case = private(state) / uuid.uuid4().hex
    save(case / "session.json", {"source": str(source), "metadata": metadata, "gaps": gaps, "events": events or fallback})
    save(case / "case.json", {"status": "inspected", "files": []})
    return {"case": str(case), "session": str(case / "session.json"), "events": len(events or fallback), "gaps": gaps}


def stage(case, sources, plan):
    require(read(case / "case.json")["status"] == "inspected", "Start a new case before staging files")
    checks = read(plan)
    require(isinstance(checks, list) and checks, "Plan must be a list of checks")
    require({c["kind"] for c in checks} == {"source", "similar", "regression"}, "Plan needs source, similar and regression checks")
    require(len({c["name"] for c in checks}) == len(checks) and all(c["name"] and c["check"] for c in checks), "Checks need unique names and descriptions")
    sources = [plain_path(source.expanduser()) for source in sources]
    require(sources, "Supply at least one file")
    bases = {p.anchor: Path(os.path.commonpath([str(s.parent) for s in sources if s.anchor == p.anchor])) for p in sources}
    groups = {anchor: f"{i:02d}" for i, anchor in enumerate(sorted(bases))}
    files, seen = [], set()
    for source in sources:
        source = plain_path(source.expanduser())
        require(source.is_file() and source not in seen, "Supply distinct existing files")
        require(not source.is_relative_to(case), "A case copy cannot be a source")
        require(not any(p.startswith(".env") or p in {".git", "auth.json", "credentials.json"} for p in source.parts)
                and source.suffix not in {".pem", ".key"}, "Do not stage credential or Git files")
        seen.add(source)
        raw = source.read_bytes()
        require(len(raw) <= 500_000 and b"\0" not in raw, "Only small UTF-8 text files are supported")
        raw.decode("utf-8-sig")
        name = groups[source.anchor] + "/" + source.relative_to(bases[source.anchor]).as_posix()
        for version in ("original", "candidate"):
            write(case / version / name, raw)
        files.append({"source": str(source), "name": name, "before": sha(source), "mode": stat.S_IMODE(source.stat().st_mode)})
    require(files, "Supply at least one file")
    save(case / "plan.json", checks)
    manifest = {"status": "staged", "files": files, "plan_sha256": sha(case / "plan.json")}
    save(case / "case.json", manifest)
    return manifest


def copy_file(case, version, entry):
    path = plain_path(case / version / entry["name"])
    require(path.is_relative_to(case / version), "Copy path escapes the case")
    return path


def seal(case):
    manifest = read(case / "case.json")
    require(manifest["status"] in {"staged", "sealed"}, "Case cannot be sealed")
    require(sha(case / "plan.json") == manifest["plan_sha256"], "Test plan changed")
    patch = []
    for entry in manifest["files"]:
        original, candidate = (copy_file(case, version, entry) for version in ("original", "candidate"))
        require(sha(original) == entry["before"], "Original copy changed")
        raw = candidate.read_bytes()
        text = raw.decode("utf-8-sig")
        require(len(raw) <= 650_000 and "\0" not in text, "Invalid candidate text")
        # PowerShell 5.1 needs an existing UTF-8 BOM preserved for non-ASCII code.
        if original.read_bytes().startswith(codecs.BOM_UTF8) and not raw.startswith(codecs.BOM_UTF8):
            write(candidate, codecs.BOM_UTF8 + raw)
        if candidate.suffix == ".py":
            compile(text, str(candidate), "exec")
        entry["after"] = sha(candidate)
        patch.extend(difflib.unified_diff(original.read_text(encoding="utf-8-sig").splitlines(True),
                                         text.splitlines(True), fromfile="original/" + entry["name"], tofile="candidate/" + entry["name"]))
    require(any(e["before"] != e["after"] for e in manifest["files"]), "No file changes")
    write(case / "changes.patch", "".join(patch).encode())
    manifest["status"] = "sealed"
    # Every seal requires a new set of test results, including when content is unchanged.
    manifest["revision"] = uuid.uuid4().hex
    save(case / "case.json", manifest)
    return {"revision": manifest["revision"], "diff": str(case / "changes.patch")}


def validate_results(case, manifest, results):
    require(results.get("revision") == manifest["revision"] and results.get("reviewed") is True, "Results must review this sealed revision")
    require(sha(case / "plan.json") == manifest["plan_sha256"], "Test plan changed")
    plan = {c["name"]: c for c in read(case / "plan.json")}
    checks = results.get("checks", [])
    require(len(checks) == len(plan) and {c["name"] for c in checks} == plan.keys(), "Results must cover every planned check")
    improved = False
    for check in checks:
        before, after = check["before"], check["after"]
        require(isinstance(before, list) and isinstance(after, list) and before and len(before) == len(after)
                and all(type(x) is bool for x in before + after), "Use equally sized lists of boolean trial results")
        require(isinstance(check.get("evidence"), str) and check["evidence"].strip(), "Record observed evidence for each check")
        require(sum(after) >= sum(before), "A planned check regressed")
        if plan[check["name"]]["kind"] == "source":
            require(all(after), "The original failure remains")
            improved |= sum(after) > sum(before)
    require(improved, "No demonstrated improvement on the original failure")


def rollback_locked(case, manifest):
    conflicts = []
    for entry in reversed(manifest["files"]):
        try:
            source = plain_path(Path(entry["source"]))
            if sha(source) == entry["before"]:
                continue
            require(sha(source) == entry["after"], "Source was edited later")
            backup = copy_file(case, "original", entry)
            require(sha(backup) == entry["before"], "Backup changed")
            write(source, backup.read_bytes(), entry["mode"])
        except (OSError, ValueError) as error:
            conflicts.append({"file": entry["source"], "error": str(error)})
    manifest.update(status="rollback_conflict" if conflicts else "rolled_back", conflicts=conflicts)
    save(case / "case.json", manifest)
    return manifest


def update_files(case, results=None, rollback=False):
    with ExitStack() as stack:
        stack.enter_context(lock(case))
        manifest = read(case / "case.json")
        for source in sorted({e["source"] for e in manifest["files"]}):
            stack.enter_context(lock(Path(source)))
        if rollback:
            require(manifest["status"] in {"applying", "applied", "rollback_conflict", "rolled_back"}, "This case has not written files")
            return rollback_locked(case, manifest)
        require(manifest["status"] == "sealed", "Seal and test the candidate before applying; roll back an interrupted apply")
        validate_results(case, manifest, results)
        for entry in manifest["files"]:
            require(sha(plain_path(Path(entry["source"]))) == entry["before"], "Source changed after staging")
            require(sha(copy_file(case, "original", entry)) == entry["before"], "Backup changed")
            require(sha(copy_file(case, "candidate", entry)) == entry["after"], "Candidate changed after sealing; seal and test again")
        save(case / "results.json", results)
        manifest["status"] = "applying"
        save(case / "case.json", manifest)
        try:
            for entry in manifest["files"]:
                if entry["before"] == entry["after"]:
                    continue
                source = plain_path(Path(entry["source"]))
                candidate = copy_file(case, "candidate", entry)
                require(sha(source) == entry["before"] and sha(candidate) == entry["after"], "Files changed during application")
                write(source, candidate.read_bytes(), entry["mode"])
            require(all(sha(Path(e["source"])) == e["after"] for e in manifest["files"]), "Applied files do not match the tested revision")
        except Exception:
            rollback_locked(case, manifest)
            raise
        manifest["status"] = "applied"
        save(case / "case.json", manifest)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    inspect = commands.add_parser("inspect")
    inspect.add_argument("session")
    inspect.add_argument("--home", type=Path, default=home)
    inspect.add_argument("--state", type=Path, default=home / "agent-repair")
    for name in ("stage", "seal", "apply", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("case", type=Path)
        if name == "stage":
            command.add_argument("files", nargs="+", type=Path)
            command.add_argument("--plan", required=True, type=Path)
        elif name == "apply":
            command.add_argument("--results", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_session(args.session, args.home.expanduser(), args.state)
        else:
            case = private(args.case)
            if args.command == "stage":
                result = stage(case, args.files, args.plan)
            elif args.command == "seal":
                result = seal(case)
            else:
                result = update_files(case, read(args.results) if args.command == "apply" else None, rollback=args.command == "rollback")
        print(json.dumps(result, ensure_ascii=False))
        return int(result.get("status") == "rollback_conflict")
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
        print(f"Error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
