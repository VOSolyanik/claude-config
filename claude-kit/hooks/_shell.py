"""Which parts of a Bash command a shell will actually execute, for the guard hooks.

Quoted arguments of ordinary commands, heredoc bodies fed to non-shells and comments are data: `git commit -m
"rm -rf old"` or `cat <<'EOF' > notes.md` must not trigger rules meant for commands. Everything a shell runs
is analysed, recursively: `bash -c`/`sh -c` strings, `eval`, `$(...)`, backticks, `<(...)`, `xargs <cmd>`,
compound commands, and heredocs or echoed strings piped into a shell (`bash <<EOF`, `echo ... | sh`).

This is a small scanner, not a shell parser. On anything it cannot balance (an unterminated quote or
substitution) it raises Unbalanced and the caller falls back to checking the whole text, which is stricter.
"""
import os
import re
import shlex

SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
PREFIXES = {"sudo", "command", "exec", "nohup", "time", "nice", "env", "builtin",
            "{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}
OPTS_WITH_ARG = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U"},
                 "env": {"-u", "-C", "-S"}, "nice": {"-n"},
                 "xargs": {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "-J", "-R"}}
REDIRECT = re.compile(r"^\d*(>>?|<|&>>?|>&|<&|>\|)$")
REDIRECT_ATTACHED = re.compile(r"^\d*(>>?|<|&>>?|>&|<&)\S+$")
VAR_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
MAX_DEPTH = 6


class Unbalanced(ValueError):
    pass


def _match_paren(text, j):
    """Index of the ')' closing the '(' at text[j], skipping quoted parts."""
    depth, k, n = 0, j, len(text)
    while k < n:
        ch = text[k]
        if ch == "\\":
            k += 2
            continue
        if ch == "'":
            k = text.find("'", k + 1)
            if k < 0:
                raise Unbalanced("quote")
        elif ch == '"':
            k += 1
            while k < n and text[k] != '"':
                k += 2 if text[k] == "\\" else 1
            if k >= n:
                raise Unbalanced("quote")
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return k
        k += 1
    raise Unbalanced("paren")


def _match_backtick(text, i):
    k = i + 1
    while k < len(text):
        if text[k] == "\\":
            k += 2
            continue
        if text[k] == "`":
            return k
        k += 1
    raise Unbalanced("backtick")


def _scan(text):
    """Split text into simple commands.

    Returns (segments, skeleton, inner, heredocs): segments are [text, separator_after] with substitutions
    replaced by `$_`; skeleton is the text with quoted literals replaced by `_`; inner are the bodies of
    substitutions (always executed); heredocs are (segment_index, body)."""
    segments, inner, heredocs, pending = [], [], [], []
    seg, skel = [], []
    i, n = 0, len(text)

    def close(sep):
        segments.append(["".join(seg), sep])
        seg.clear()
        skel.append(sep if sep else "")

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "\\":
            seg.append(text[i:i + 2]); skel.append(text[i:i + 2]); i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise Unbalanced("quote")
            seg.append(text[i:j + 1]); skel.append("'_'"); i = j + 1
        elif c == '"':
            k, buf = i + 1, ['"']
            while k < n and text[k] != '"':
                if text[k] == "\\":
                    buf.append(text[k:k + 2]); k += 2
                elif text[k] == "$" and text[k + 1:k + 2] == "(":
                    end = _match_paren(text, k + 1)
                    inner.append(text[k + 2:end]); buf.append("$_"); k = end + 1
                elif text[k] == "`":
                    end = _match_backtick(text, k)
                    inner.append(text[k + 1:end]); buf.append("$_"); k = end + 1
                else:
                    buf.append(text[k]); k += 1
            if k >= n:
                raise Unbalanced("quote")
            seg.append("".join(buf) + '"'); skel.append('"_"'); i = k + 1
        elif (c == "$" or c in "<>") and nxt == "(" and not (c == "<" and text[i - 1:i] == "<"):
            end = _match_paren(text, i + 1)
            inner.append(text[i + 2:end]); seg.append("$_"); skel.append("$_"); i = end + 1
        elif c == "`":
            end = _match_backtick(text, i)
            inner.append(text[i + 1:end]); seg.append("$_"); skel.append("$_"); i = end + 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("<<<", i):
            seg.append("<<<"); skel.append("<<<"); i += 3
        elif text.startswith("<<", i):
            j = i + 2
            strip_tabs = text[j:j + 1] == "-"
            j += 1 if strip_tabs else 0
            while j < n and text[j] in " \t":
                j += 1
            if j < n and text[j] in "'\"":
                end = text.find(text[j], j + 1)
                if end < 0:
                    raise Unbalanced("heredoc delimiter")
                delim, j = text[j + 1:end], end + 1
            else:
                start = j
                while j < n and text[j] not in " \t\n;&|<>()":
                    j += 1
                delim = text[start:j].replace("\\", "")
            pending.append((len(segments), delim, strip_tabs))
            seg.append(" "); skel.append(" "); i = j
        elif c == "\n":
            close("\n")
            i += 1
            for idx, delim, strip_tabs in pending:
                body = []
                while i < n:
                    end = text.find("\n", i)
                    line = text[i:] if end < 0 else text[i:end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
                    body.append(line)
                heredocs.append((idx, "\n".join(body)))
            pending = []
        elif text.startswith("&&", i) or text.startswith("||", i):
            close(text[i:i + 2]); i += 2
        elif c == "&" and (text[i - 1:i] in "<>" and i > 0 or nxt == ">"):
            seg.append(c); skel.append(c); i += 1
        elif c in ";|&()":
            close(c); i += 1
        else:
            seg.append(c); skel.append(c); i += 1
    close(None)
    return segments, "".join(skel), inner, heredocs


def _strip_prefix(toks):
    """Drop assignments, wrappers (sudo, env, ...) and their options, and keywords before the command."""
    while toks:
        head = toks[0]
        if VAR_ASSIGN.match(head):
            toks = toks[1:]
        elif head in PREFIXES:
            with_arg = OPTS_WITH_ARG.get(head, set())
            toks = toks[1:]
            while toks and toks[0].startswith("-") and head in ("sudo", "env", "nice"):
                toks = toks[2:] if toks[0] in with_arg else toks[1:]
        else:
            break
    return toks


def _drop_redirects(toks):
    out, skip = [], False
    for t in toks:
        if skip:
            skip = False
        elif REDIRECT.match(t):
            skip = True
        elif not REDIRECT_ATTACHED.match(t):
            out.append(t)
    return out


def _tokens(segment):
    try:
        toks = shlex.split(segment, posix=True)
    except ValueError:
        toks = segment.split()
    return _drop_redirects(_strip_prefix(toks))


def _shell_role(toks):
    """('c', script) for `sh -c script`, ('stdin', None) for a shell reading stdin, else (None, None)."""
    for k, t in enumerate(toks[1:], 1):
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return "c", toks[k + 1] if k + 1 < len(toks) else ""
        if not t.startswith("-"):
            return None, None  # a script file: its content is not ours to see
    return "stdin", None


def _command(toks, queue, commands):
    """Record one simple command; queue whatever it will execute itself."""
    while toks and os.path.basename(toks[0]) == "xargs":
        k = 1
        while k < len(toks) and toks[k].startswith("-"):
            k += 2 if toks[k] in OPTS_WITH_ARG["xargs"] else 1
        toks = _strip_prefix(toks[k:])
    if not toks:
        return None
    commands.append(toks)
    name = os.path.basename(toks[0])
    if name in SHELLS:
        role, script = _shell_role(toks)
        if role == "c":
            queue.append(script)
        return role
    if name == "eval":
        queue.append(" ".join(toks[1:]))
    return None


def executed_parts(cmd):
    """(commands, skeletons, pipelines): token lists of every simple command a shell would run (unquoted),
    the text of every executed piece with quoted literals blanked, and the commands of every pipeline of two
    or more. Raises Unbalanced on text it cannot balance."""
    commands, skeletons, pipelines, queue = [], [], [], [(cmd, 0)]
    while queue:
        text, depth = queue.pop()
        if depth > MAX_DEPTH or not text.strip():
            continue
        segments, skeleton, inner, heredocs = _scan(text)
        skeletons.append(skeleton)
        found = list(inner)
        pipeline = []
        for idx, (segment, sep) in enumerate(segments):
            toks = _tokens(segment)
            role = _command(toks, found, commands)
            pipeline.append((idx, toks, role))
            if sep == "|":
                continue
            if len(pipeline) > 1:
                pipelines.append([toks_ for _, toks_, _ in pipeline])
            if any(role == "stdin" for _, _, role in pipeline):
                members = {i for i, _, _ in pipeline}
                found.extend(body for i, body in heredocs if i in members)
                for _, toks_, role_ in pipeline:
                    if role_ != "stdin":
                        found.extend(toks_[1:])
            pipeline = []
        queue.extend((t, depth + 1) for t in found)
    return commands, skeletons, pipelines
