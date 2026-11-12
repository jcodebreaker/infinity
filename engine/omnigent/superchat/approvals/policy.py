"""Approvals: the person is asked before anything that acts in their name or spends their money.

Six risk categories (``send``, ``post``, ``delete``, ``spend``, ``upload``, ``share``) are
recognised from a tool call by :func:`classify_tool_call`: shell commands that send, delete or
upload, browser clicks on buy/send/post/delete buttons (by the clicked element's name), publish
tools, and connector tools (email, chat, payments, drive) by name. Further recognisers plug in
with :func:`register_classifier`.

A classified call is ASKed (an elicitation, never a timeout-to-yes) unless one of the owner's
standing rules covers every target of the call. A ``spend`` rule is honoured only while the
owner's daily spending cap (default ``0`` = always ask) has room for the call's amount, so
"always allow" can never exceed the cap. An unattended run (the Lookout's wake, the brief) has
nobody to ask: its classified calls are denied; its reads stay unclassified and allowed.

The policy is installed by the engine on Super Chat sessions (see
``omnigent.runtime.policies.builder``); the rules store is bound at server start with
:func:`configure_store`. Without a store (runner-local evaluation) every classified call ASKs and
the server re-evaluates it with the rules.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlparse

from omnigent.context.labels import UNATTENDED_LABEL_KEY, is_superside_chat
from omnigent.entities import Conversation
from omnigent.policies.schema import PolicyEvent, PolicyResponse
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from omnigent.superchat.risk import (  # noqa: F401  (re-exported: the classifier registry's old home)
    Classifier,
    Risk,
    _extra_classifiers,
    register_classifier,
)

POLICY_NAME = "__muse_approvals"

CATEGORIES = ("send", "post", "delete", "spend", "upload", "share")

# An approval waits for the person for as long as it takes: a week, never a yes.
ASK_TIMEOUT_SECONDS = 7 * 24 * 3600

REASON_MARK = "[[approval]]"

#: The DENY an unattended run (``UNATTENDED_LABEL_KEY``) gets for a classified call; worded as a
#: policy notice so a Result that repeats it is never posted.
UNATTENDED_DENY = (
    "Blocked by policy: this run works unattended, so it never sends, posts, deletes, spends, "
    "uploads or shares. Prepare it as a draft for the person instead."
)

_VERB = {
    "send": "Send a message",
    "post": "Post publicly",
    "delete": "Delete",
    "spend": "Spend money",
    "upload": "Upload your files",
    "share": "Share your information",
}
_RULE_LABEL = {
    "send": "Send messages to {t}",
    "post": "Post to {t}",
    "delete": "Delete {t}",
    "spend": "Spend on {t}",
    "upload": "Upload files to {t}",
    "share": "Share with {t}",
}


def rule_label(category: str, target: str) -> str:
    """The sentence Settings shows for a standing rule."""
    return _RULE_LABEL.get(category, "{t}").format(t=target)


# ── encoding of the structured fields into the elicitation message ────────────


def encode_reason(risk: Risk, owner: str | None, note: str = "") -> str:
    """The ASK reason: a machine header line plus the sentence the person reads."""
    header = {
        "c": risk.category,
        "t": list(risk.targets),
        "s": risk.summary,
        "a": risk.amount_usd,
        "u": owner,
    }
    text = risk.summary + (f" {note}" if note else "")
    return f"{REASON_MARK}{json.dumps(header, separators=(',', ':'))}\n{text}"


def decode_reason(message: object) -> dict[str, Any] | None:
    """Parse :func:`encode_reason`; ``None`` when the message is not an approval.

    Strict: the mark must open the message. Pass this policy's own reason (the engine keeps
    it per policy), never a composed one, where other policies' text could imitate a header.
    """
    if not isinstance(message, str) or not message.startswith(REASON_MARK):
        return None
    head = message[len(REASON_MARK) :].split("\n", 1)[0]
    try:
        parsed = json.loads(head)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("c") not in CATEGORIES:
        return None
    return parsed


# ── classification ────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"(?:https?|smtps?|ftp|sftp)://[^\s'\"<>]+", re.I)
_MONEY_RE = re.compile(
    r"(?:[$€£]\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"|\b(?:usd|eur|gbp|cad)\s?(\d+(?:\.\d{1,2})?))",
    re.I,
)
_SAFE_DELETE_ROOTS = ("/tmp/", "/var/tmp/", "/private/tmp/", "/private/var/folders/")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}

_SHELL_TOOLS = {
    "sys_os_shell",
    "bash",
    "shell",
    "terminal",
    "developer__shell",
    "sys_terminal_run",
}

_MESSAGING_HOSTS = (
    "hooks.slack.com",
    "slack.com",
    "discord.com",
    "discordapp.com",
    "api.telegram.org",
    "api.twilio.com",
    "api.sendgrid.com",
    "api.mailgun.net",
    "api.resend.com",
    "api.postmarkapp.com",
    "api.sparkpost.com",
    "gmail.googleapis.com",
    "graph.microsoft.com",
    "graph.facebook.com",
    "api.whatsapp.com",
)
_POSTING_HOSTS = (
    "api.twitter.com",
    "api.x.com",
    "api.linkedin.com",
    "bsky.social",
    "mastodon.social",
    "reddit.com",
    "dev.to",
    "api.medium.com",
)
_PAYMENT_HOSTS = ("api.stripe.com", "paypal.com", "api-m.paypal.com", "squareup.com", "plaid.com")
_MAIL_PROGRAMS = {"sendmail", "mail", "mailx", "mutt", "neomutt", "msmtp", "swaks", "mpack"}
_PUBLISH_PROGRAMS = {"vercel", "netlify", "wrangler", "firebase", "surge", "twine"}
_TUNNEL_PROGRAMS = {"ngrok", "cloudflared", "localtunnel", "lt", "bore", "serveo"}
_WRAPPERS = {"sudo", "command", "nohup", "time", "env", "exec", "nice", "xargs", "builtin"}
_SPLIT_RE = re.compile(r"\|\||&&|[;|\n]")
_UPLOAD_FLAGS = {"-f", "--form", "-t", "--upload-file", "--post-file", "--data-binary"}


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, names: tuple[str, ...]) -> bool:
    return any(host == n or host.endswith("." + n) for n in names)


def _money(text: str) -> float | None:
    match = _MONEY_RE.search(text)
    if match is None:
        return None
    raw = match.group(1) or match.group(2)
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    for part in _SPLIT_RE.split(command):
        try:
            tokens = shlex.split(part, posix=True)
        except ValueError:
            tokens = part.split()
        while tokens and (tokens[0] in _WRAPPERS or re.fullmatch(r"\w+=\S*", tokens[0])):
            tokens = tokens[1:]
        if tokens:
            out.append(tokens)
    return out


def _shell_segment_risk(tokens: list[str], text: str) -> Risk | None:
    prog = os.path.basename(tokens[0]).lower()
    args = tokens[1:]
    lowered = [a.lower() for a in args]
    urls = [u for t in tokens for u in _URL_RE.findall(t)]
    hosts = [h for h in (_host(u) for u in urls) if h and h not in _LOCAL_HOSTS]
    low_text = text.lower()

    # delete
    if prog in {"rm", "rmdir", "unlink", "shred", "trash", "srm"}:
        paths = [a for a in args if not a.startswith("-")]
        # Normalised first: ``/tmp/../home/x`` is not under /tmp.
        if paths and all(os.path.normpath(p).startswith(_SAFE_DELETE_ROOTS) for p in paths):
            return None
        return Risk(
            "delete", tuple(paths[:3]) or (prog,), f"Delete {', '.join(paths[:3]) or 'files'}"
        )
    if prog == "find" and ("-delete" in args or ("-exec" in args and "rm" in args)):
        return Risk(
            "delete", (args[0] if args else ".",), f"Delete files under {args[0] if args else '.'}"
        )
    if prog == "git" and "clean" in lowered:
        return Risk("delete", ("untracked files",), "Delete untracked files")
    if prog in {"docker", "podman"} and {"rm", "rmi", "prune"} & set(lowered):
        return Risk("delete", ("containers",), "Delete containers or images")
    if prog in {"kubectl", "aws", "gcloud", "gh", "az", "doctl", "heroku", "fly", "flyctl"} and (
        {"delete", "rm", "terminate", "destroy", "remove"} & set(lowered)
    ):
        return Risk("delete", (prog,), f"Delete something with {prog}")
    if re.search(r"\b(drop\s+(table|database)|delete\s+from|truncate\s+table)\b", low_text):
        return Risk("delete", ("database",), "Delete database records")

    # send
    if prog in _MAIL_PROGRAMS:
        who = tuple(dict.fromkeys(_EMAIL_RE.findall(text))) or (prog,)
        return Risk("send", who, f"Send an email to {', '.join(who)}")
    if prog == "osascript" and re.search(r'application\s+"(mail|messages)"', low_text):
        return Risk("send", ("Mail/Messages",), "Send a message from your machine")
    if re.search(r"\b(smtplib|chat\.postmessage|sendmessage)\b", low_text):
        return Risk("send", ("messaging",), "Send a message")

    # upload
    if prog in {"curl", "wget", "http", "https", "xh"} and hosts:
        host = hosts[0]
        uploads = bool(_UPLOAD_FLAGS & set(lowered)) or any(
            a.startswith(("-d@", "--data@", "--data-binary@", "@")) or "=@" in a for a in args
        )
        data_file = any(
            a in {"-d", "--data", "--data-raw", "--data-binary", "--json"}
            and i + 1 < len(args)
            and args[i + 1].startswith("@")
            for i, a in enumerate(args)
        )
        posting = bool(
            ({"-x", "--request"} & set(lowered) and ({"post", "put", "patch"} & set(lowered)))
            or {"-d", "--data", "--data-raw", "--json"} & set(lowered)
            or uploads
            or data_file
        )
        if _host_matches(host, _PAYMENT_HOSTS) and posting:
            return Risk("spend", (host,), f"Make a payment through {host}", _money(text))
        if _host_matches(host, _MESSAGING_HOSTS) and posting:
            return Risk("send", (host,), f"Send a message through {host}")
        if _host_matches(host, _POSTING_HOSTS) and posting:
            return Risk("post", (host,), f"Post publicly on {host}")
        if uploads or data_file:
            return Risk("upload", (host,), f"Upload your files to {host}")
    if prog in {"scp", "sftp", "rsync", "ftp"} and any(
        re.match(r"^(?:[\w.-]+@)?[\w.-]+:[^\s]*$", a) and not a.startswith("-") for a in args
    ):
        remote = next(a for a in args if re.match(r"^(?:[\w.-]+@)?[\w.-]+:", a))
        return Risk("upload", (remote.split(":")[0],), f"Copy files to {remote.split(':')[0]}")
    if prog in {"aws", "gsutil", "gcloud", "rclone", "az"} and (
        {"cp", "mv", "sync", "copy", "move", "copyto", "upload"} & set(lowered)
    ):
        remote = next((a for a in args if re.match(r"^(s3|gs)://", a)), None)
        if remote or prog in {"rclone", "az"}:
            dest = remote or prog
            return Risk("upload", (dest,), f"Upload your files to {dest}")
    if prog == "gh" and "gist" in lowered and "create" in lowered:
        return Risk("upload", ("github gist",), "Upload your files to a GitHub gist")

    # post
    if (
        prog == "gh"
        and ({"issue", "pr", "release", "discussion"} & set(lowered))
        and ({"create", "comment", "review", "edit"} & set(lowered))
    ):
        return Risk("post", ("github",), "Post on GitHub")
    if prog == "git" and "push" in lowered:
        return Risk("post", ("git remote",), "Push code to a remote")
    if prog in {"npm", "yarn", "pnpm", "cargo", "gem", "twine"} and (
        "publish" in lowered or "upload" in lowered
    ):
        return Risk("post", (prog,), f"Publish a package with {prog}")
    if prog in _PUBLISH_PROGRAMS and ({"deploy", "publish", "upload", "--prod"} & set(lowered)):
        return Risk("post", (prog,), f"Publish a site with {prog}")

    # spend
    if prog == "stripe" or (
        prog in {"aws", "gcloud", "doctl"}
        and {"run-instances", "create", "purchase"} & set(lowered)
        and {"ec2", "compute", "droplet"} & set(lowered)
    ):
        return Risk("spend", (prog,), f"Spend money with {prog}", _money(text))

    # share
    if prog in _TUNNEL_PROGRAMS or (prog == "tailscale" and "funnel" in lowered):
        return Risk("share", (prog,), "Make something on your machine reachable from the internet")
    if prog == "gh" and "repo" in lowered and "--visibility" in lowered and "public" in lowered:
        return Risk("share", ("github repo",), "Make a repository public")
    return None


def _command_of(args: Mapping[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(str(v) for v in value)
    return ""


def classify_shell(command: str) -> Risk | None:
    """Recognise a shell command that sends, deletes, uploads, posts, spends or shares."""
    for tokens in _segments(command):
        risk = _shell_segment_risk(tokens, " ".join(tokens))
        if risk is not None:
            return risk
    return None


# Browser clicks: the clicked element's name decides. First match wins; order matters.
_CLICK_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "spend",
        re.compile(
            r"\b(buy|purchase|pay|place (?:your )?order|checkout|check out|subscribe|donate|"
            r"complete (?:order|purchase)|confirm (?:order|purchase|payment))\b",
            re.I,
        ),
    ),
    (
        "delete",
        re.compile(r"\b(delete|remove|discard|erase|destroy|deactivate|close account)\b", re.I),
    ),
    ("send", re.compile(r"\b(send|reply|reply all|forward|submit message)\b", re.I)),
    ("post", re.compile(r"\b(post|publish|tweet|share publicly|go live)\b", re.I)),
    ("upload", re.compile(r"\bupload\b", re.I)),
    ("share", re.compile(r"\b(share|invite|grant access)\b", re.I)),
)
_CLICK_VERB = {
    "spend": "Buy something",
    "delete": "Delete something",
    "send": "Send a message",
    "post": "Post something publicly",
    "upload": "Upload your files",
    "share": "Share something",
}

# Per-session memory of the last browser snapshot: ``ref -> element line``, plus the page host.
_snapshots: dict[str, dict[str, Any]] = {}
_SNAPSHOT_LIMIT = 256
_REF_LINE_RE = re.compile(r"\[ref=(\d+)\]")


def remember_browser_snapshot(session_id: str | None, tool_name: str, output: object) -> None:
    """Record a ``browser_snapshot``/click/type result so a later click can be named.

    :param session_id: The session the tool ran in.
    :param tool_name: The browser tool (any ``browser_*``; others are ignored).
    :param output: Its raw result, a JSON string or dict with ``tree`` and ``url``.
    """
    if not session_id or not tool_name.rsplit("__", 1)[-1].startswith("browser_"):
        return
    data: object = output
    if isinstance(output, str):
        try:
            data = json.loads(output)
        except ValueError:
            return
    if not isinstance(data, dict) or not isinstance(data.get("tree"), str):
        return
    refs: dict[int, str] = {}
    for line in data["tree"].splitlines():
        match = _REF_LINE_RE.search(line)
        if match:
            refs[int(match.group(1))] = _REF_LINE_RE.sub("", line).strip(" -\t")
    host = _host(data["url"]) if isinstance(data.get("url"), str) else ""
    if len(_snapshots) >= _SNAPSHOT_LIMIT and session_id not in _snapshots:
        _snapshots.pop(next(iter(_snapshots)))
    _snapshots[session_id] = {"refs": refs, "host": host, "at": time.time()}


def _browser_click_risk(args: Mapping[str, Any], session_id: str | None) -> Risk | None:
    snapshot = _snapshots.get(session_id or "")
    name = ""
    ref = args.get("ref")
    if snapshot is not None and isinstance(ref, int):
        name = snapshot["refs"].get(ref, "")
    selector = args.get("selector")
    if not name and isinstance(selector, str):
        name = selector
    explicit = args.get("target_name")
    if isinstance(explicit, str):
        name = explicit
    if not name:
        return None
    host = (snapshot or {}).get("host") or "this page"
    for category, pattern in _CLICK_WORDS:
        if pattern.search(name):
            quoted = re.sub(r"\s+", " ", name)[:80]
            return Risk(
                category,
                (host,),
                f"{_CLICK_VERB[category]} on {host}: {quoted}",
                _money(name) if category == "spend" else None,
            )
    return None


_CONNECTOR_TOKENS = frozenset(
    [
        "gmail",
        "email",
        "mail",
        "outlook",
        "slack",
        "discord",
        "telegram",
        "whatsapp",
        "sms",
        "twilio",
        "twitter",
        "linkedin",
        "facebook",
        "instagram",
        "stripe",
        "paypal",
        "payment",
        "payments",
        "drive",
        "gdrive",
        "dropbox",
        "notion",
    ]
)
_NAME_VERBS: tuple[tuple[str, frozenset[str]], ...] = (
    ("spend", frozenset(["pay", "purchase", "buy", "checkout", "charge", "transfer"])),
    ("delete", frozenset(["delete", "remove", "destroy", "purge", "erase", "trash"])),
    ("send", frozenset(["send", "reply", "forward"])),
    ("post", frozenset(["post", "publish", "tweet", "deploy"])),
    ("upload", frozenset(["upload"])),
    ("share", frozenset(["share"])),
)
_DELETE_NOUNS = frozenset(["artifact", "artifacts", "file", "files", "document", "doc", "library"])


def _classify_by_name(name: str, tokens: set[str], connector: bool) -> Risk | None:
    for category, verbs in _NAME_VERBS:
        if not verbs & tokens:
            continue
        if category == "post" and "publish" not in tokens and not connector:
            continue
        if category == "delete" and not (connector or _DELETE_NOUNS & tokens):
            continue
        if category not in ("post", "delete") and not connector:
            continue
        return Risk(category, (name,), f"{_VERB[category]} ({name.replace('_', ' ')})")
    return None


def classify_tool_call(
    tool_name: str, arguments: object, *, session_id: str | None = None
) -> Risk | None:
    """Which risk category, if any, a tool call falls in.

    :param tool_name: The tool, possibly ``mcp__<server>__`` prefixed.
    :param arguments: Parsed arguments (dict) or their JSON text.
    :param session_id: The session, used to name browser click targets.
    :returns: The :class:`Risk`, or ``None`` for an ordinary call.
    """
    args: Mapping[str, Any]
    if isinstance(arguments, str):
        try:
            loaded = json.loads(arguments)
        except ValueError:
            loaded = {}
        args = loaded if isinstance(loaded, dict) else {}
    elif isinstance(arguments, Mapping):
        args = arguments
    else:
        args = {}
    external_mcp = tool_name.startswith("mcp__") and not tool_name.startswith("mcp__omnigent__")
    base = tool_name.rsplit("__", 1)[-1] if tool_name.startswith("mcp__") else tool_name
    lowered = base.lower()
    for classifier in _extra_classifiers:
        risk = classifier(lowered, args)
        if risk is not None:
            return risk
    if lowered in _SHELL_TOOLS:
        return classify_shell(_command_of(args))
    if lowered == "browser_click":
        return _browser_click_risk(args, session_id)
    if lowered.startswith(("sys_", "memory_", "browser_", "goal_", "objective_")):
        return None
    tokens = {t for t in re.split(r"[_\-.\s]+", lowered) if t}
    connector = external_mcp or bool(_CONNECTOR_TOKENS & tokens)
    return _classify_by_name(base, tokens, connector)


# ── the policy ────────────────────────────────────────────────────────────────

_store: Any = None


def configure_store(store: Any | None) -> None:
    """Bind (or clear) the :class:`SqlAlchemyApprovalStore` the policy reads rules from."""
    global _store
    _store = store


def get_store() -> Any | None:
    """The bound approval store, or ``None``."""
    return _store


def applies(conversation: Conversation | None, tree: list[Conversation] | None) -> bool:
    """Whether the approvals policy covers a session: a store is bound and it is a Super Chat's.

    The Super Chat's tree is the Conversation and its Helpers. With no *tree* (the cheap
    pre-check) a child may still belong to one, so it counts; the engine build decides.
    """
    if _store is None:
        return False
    if conversation is None:
        return True
    if tree is None:
        return (
            is_superside_chat(conversation.labels)
            or conversation.parent_conversation_id is not None
        )
    return any(is_superside_chat(row.labels) for row in [conversation, *tree])


#: Asks before a Super Chat session sends, posts, deletes, spends, uploads or shares; installed
#: through the policy builder's hooks (``omnigent.superchat.features``) on the sessions
#: :func:`applies` covers.
POLICY_SPEC = FunctionPolicySpec(
    name=POLICY_NAME,
    on=None,
    function=FunctionRef(
        path="omnigent.superchat.approvals.policy.muse_approvals", arguments=None
    ),
    ask_timeout=ASK_TIMEOUT_SECONDS,
)


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _covered(rules: list[Any], category: str, targets: tuple[str, ...], decision: str) -> bool:
    patterns = [
        r.target.lower() for r in rules if r.category == category and r.decision == decision
    ]
    return bool(targets) and all(any(fnmatchcase(t.lower(), p) for p in patterns) for t in targets)


def muse_approvals(event: PolicyEvent) -> PolicyResponse | None:
    """ASK before a send/post/delete/spend/upload/share call unless a standing rule allows it.

    :param event: Policy event; only ``tool_call`` is considered.
    :returns: ASK with the approval reason, ALLOW (a covering rule), DENY (a ``deny`` rule, or
        any classified call of an unattended run), or ``None`` for an ordinary call.
    """
    if event.get("type") != "tool_call":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str):
        return None
    context = event.get("context") or {}
    session_id = context.get("conversation_id") if isinstance(context, dict) else None
    risk = classify_tool_call(name, data.get("arguments"), session_id=session_id)
    if risk is None:
        return None
    labels = context.get("labels") if isinstance(context, dict) else None
    if isinstance(labels, dict) and labels.get(UNATTENDED_LABEL_KEY):
        # Nobody is there to answer (ADR 0038): deny, never ask, whatever the standing rules.
        # Reads and the run's own draft are unclassified, so they never reach here.
        return {"result": "DENY", "reason": UNATTENDED_DENY}
    actor = context.get("actor") if isinstance(context, dict) else None
    owner = actor.get("run_as") if isinstance(actor, dict) else None
    owner = owner if isinstance(owner, str) and owner else "local"
    note = ""
    store = _store
    if store is not None:
        rules = store.list_rules(owner)
        if _covered(rules, risk.category, risk.targets, "deny"):
            return {"result": "DENY", "reason": "You told me never to do this."}
        if _covered(rules, risk.category, risk.targets, "allow"):
            if risk.category != "spend":
                return {"result": "ALLOW"}
            cap = store.get_cap(owner)
            spent = store.get_spend(owner, _today())
            if risk.amount_usd is not None and cap > 0 and spent + risk.amount_usd <= cap:
                store.add_spend(owner, _today(), risk.amount_usd)
                return {"result": "ALLOW"}
            note = "(Over your daily spending limit.)" if cap > 0 else ""
    return {"result": "ASK", "reason": encode_reason(risk, owner, note)}


POLICY_REGISTRY: list[dict[str, Any]] = []
