"""Pure command parsing shared by plugin entry points; no host imports."""

import shlex


def tokenize(text):
    lexer = shlex.shlex(text or "", posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""  # Backslashes in Windows paths are literal, including in quotes.
    return list(lexer)


def parse_run_args(tokens):
    project, params = None, {}
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            if not key:
                raise ValueError(f"invalid param {token!r}: expected name=value")
            params[key] = value
        elif project is None:
            project = token
        else:
            raise ValueError(f"unexpected argument {token!r}: use name=value for params")
    return project, params


def resolve_nl_command(text, workflows):
    cleaned = (text or "").strip()
    if not cleaned:
        return {"question": "Which workflow should I run? Try `/workflow <id> <instruction>`."}
    best, matches = 0, []
    for workflow in workflows:
        for target in (str(workflow.get("id", "")), str(workflow.get("name", ""))):
            target = target.strip().lower()
            if target and (cleaned.lower() == target or cleaned.lower().startswith(target + " ")):
                remainder = cleaned[len(target) :].strip()
                if len(target) > best:
                    best, matches = len(target), [(str(workflow["id"]), remainder)]
                elif len(target) == best:
                    matches.append((str(workflow["id"]), remainder))
    if not matches:
        available = ", ".join(sorted({str(w["id"]) for w in workflows})) or "(none configured)"
        return {
            "question": f"I could not match a workflow in {cleaned!r}. Which one did you "
            f"mean? Available: {available}. Try `/workflow <id> <instruction>`."
        }
    unique = {identity for identity, _ in matches}
    if len(unique) > 1:
        return {
            "question": f"That could be any of: {', '.join(sorted(unique))}. "
            "Which workflow did you mean?"
        }
    identity, remainder = matches[0]
    return {"workflow_id": identity, "input": remainder or None}


def parse_resume_args(tokens):
    node, reset_all = None, False
    if not tokens:
        return node, reset_all
    if tokens == ["--all"]:
        return None, True
    if (
        len(tokens) == 2 and tokens[0] in ("--node", "--node-id")
        and tokens[1].strip() and not tokens[1].startswith("--")
    ):
        return tokens[1], False
    raise ValueError("resume expects either --node <id> or --all")
