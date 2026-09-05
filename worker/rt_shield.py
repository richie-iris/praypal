"""rt_shield.py — ScamGuard shielding, privacy state, and speech intent detection.

Extracts intent classifiers, panic detection, and ScamGuard helper predicates from agent.py.
"""
from __future__ import annotations

import re
import unicodedata

_PANIC_PHRASES = (
    "hang up on him", "hang up on her", "hang up on them", "hang up on this",
    "get rid of him", "get rid of her", "get rid of them", "get him off",
    "get her off", "get them off", "make it stop", "make him stop",
    "end this call", "i want to hang up", "drop the call", "hang up now",
    "just hang up",
)

_FAREWELL = re.compile(
    r"(?i)\s*(ok(ay)?[ ,]*)?(well[ ,]*)?(alright[ ,]*)?(thanks?( you)?[ ,]*)?"
    r"(good ?bye|bye|bye bye|bye now|"
    r"talk (to you )?later|see you( later)?|gotta go|i have to go|"
    r"i'?m going to (go|hang up)|let'?s hang up|i'?ll let you go|"
    r"have a (good|nice|lovely|great|wonderful|blessed) (day|night|one|evening|weekend))"
    r"([ ,]*(now|then|dear|hon(ey)?|[a-z]{2,12})){0,2}\s*"
)

_NOT_A_FAREWELL = re.compile(
    r"\b(if|unless|don'?t|do not|before|when|whether|said|says|say|told|asked|"
    r"he|she|they|we|used to|should|would|could|maybe)\b",
    re.I,
)

_QUIET_REQUESTS = (
    "just listen", "listen only", "only listen", "listen in", "don't talk",
    "do not talk", "stop talking", "stay quiet", "be quiet", "keep quiet",
    "don't say anything", "no need to talk", "hang back", "stay out of it",
)

_COMMON_WORDS = frozenset("""
been being name named names think thing things there their they that this with what
when where which would could should about right really little listen again alright
okay yeah sure please thanks thank hello hey morning afternoon evening night today
tomorrow yesterday actually anything everything something nothing because before
after still first last next call called calling caller phone number numbers remember
remembered doctor nurse office hospital pharmacy insurance appointment money card
cards said say says tell told talk talking talked know knows knew good great fine
well were was are you your yours mine ours them him her his she him hers just like
want wants need needs help helps helping give gives given take takes come comes
going gone here have has had did does done make makes made from into over under
much many more most some none other another each every all any own same than then
""".split())


def _panic_phrase_present(text: str) -> bool:
    """True if caller utterance matches emergency disconnect request."""
    low = " ".join((text or "").lower().split())
    return any(p in low for p in _PANIC_PHRASES)


def _is_farewell(text: str) -> bool:
    """True only when the WHOLE utterance is a goodbye."""
    t = " ".join((text or "").split()).strip().rstrip(".!,")
    if not t or len(t.split()) > 6:
        return False
    if _NOT_A_FAREWELL.search(t):
        return False
    return bool(_FAREWELL.fullmatch(t))


def _asked_for_quiet(text: str) -> bool:
    """True only when the caller actually asked her to go silent."""
    low = " ".join((text or "").lower().split())
    return any(p in low for p in _QUIET_REQUESTS)


def _wake_word_present(text: str, alias: str | None) -> bool:
    """True when the caller addressed her by name — her cue to answer."""
    words = {(alias or "").strip().lower()} - {"", "your companion"}
    low = (text or "").lower()
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)


def _is_synthetic_line(text: str) -> bool:
    """True for injected control turns that must never enter the transcript."""
    return text.strip().lower().startswith("(call just connected")


def _heard_in_caller_lines(name: str, transcript_lines: list[str]) -> bool:
    """True if `name` appears in at least one caller-spoken line."""
    n = (name or "").strip().lower()
    if not n or n in ("friend", "unknown") or n in _COMMON_WORDS:
        return False
    for line in transcript_lines:
        words = re.findall(r"\b\w+\b", line.lower())
        if n in words:
            return True
    return False


# --- caller-rule ingestion (finding #3) ------------------------------------
# A caller_rules row is rendered straight into the system prompt, so it is the
# one place a scammer on the line could plant an instruction that outlives the
# call. Refuse anything that reads like prompt steering or names a tool; the
# limit keeps a "rule" from becoming a second prompt. Word boundaries keep
# ordinary speech ("developers", "guarded") out of the net.
_RULE_MAX_LEN = 300
_RULE_TOOL_NAMES = (
    "bridge_call", "send_email", "send_sms", "db_tool", "forget_me", "web_search",
    "find_number", "schedule_reminder_call", "manage_goals", "press_keys", "end_call",
    "listen_only", "end_bridge", "save_email", "recall_earlier", "send_calendar_invite",
)
_RULE_BLOCKED_PHRASES = (
    "ignore", "disregard", "previous instructions", "system prompt", "never break",
    "always say", "you are now", "developer", "override", "jailbreak", "vault",
    "password", "social security", "ssn", "scam", "shield", "guard",
    # obedience shape: a rule that hands the wheel to whoever is on the line
    "obey", "do as", "do whatever", "comply", "cooperate", "tell you", "tells you",
    "say so", "says so",
) + _RULE_TOOL_NAMES
_RULE_BLOCKED = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _RULE_BLOCKED_PHRASES) + r")\b"
    r"|\bpin[ .]"  # "pin " / "pin." — the credential, not "spinning"
    r"|\btreat\b.{0,40}?\bas me\b",  # "treat my nephew as me"
    re.I | re.S,
)
# A rule can also leak without naming a tool or a credential: "read my codes
# to whoever asks". Refuse a disclosure verb within 40 chars of either a
# listener ("anyone", "who asks") or a data noun ("codes", "on file"), in
# either order. Ordinary preferences ("speak slowly", "use my first name")
# carry neither half.
_DISCLOSE_VERB = (
    r"\b(?:read|tell|share|give|reveal|say|recite|repeat|disclose|confirm|provide"
    r"|spell out|hand over|state)\b"
)
_DISCLOSE_TARGET = (
    r"\b(?:anyone|anybody|whoever|whomever|someone|everyone|everybody|callers?|they ask"
    r"|asks|asked|who asks|who calls|who rings|who phones"
    r"|codes?|notes?|file|on file|details|information|address|bank|account|medication"
    r"|credentials|password|category|vault|records?)\b"
)
_RULE_DISCLOSURE = re.compile(
    rf"(?:{_DISCLOSE_VERB}.{{0,40}}?{_DISCLOSE_TARGET})|(?:{_DISCLOSE_TARGET}.{{0,40}}?{_DISCLOSE_VERB})",
    re.I | re.S,
)


# Unicode-aware word token (letters, digits — not "_"), taken from the
# NFKC-folded, mark-stripped text so fullwidth or zero-width-split words read
# as themselves and a bare combining mark can never split a word in two.
_WORD = re.compile(r"[^\W_]+")

# Typographic punctuation that STT and the model both emit for the ASCII
# marks the allowlist below admits. NFKC leaves these alone, so fold them
# here: a caller's "don’t" is the ASCII "don't", not a foreign character.
_TYPOGRAPHIC_FOLD = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2010": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
})


def _normalise_rule(text: str) -> str:
    """NFKC-fold, drop format chars and fold curly quotes/dashes so "ig​nore" or fullwidth letters cannot slip past the regexes."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf").translate(_TYPOGRAPHIC_FOLD)


# A rule is rendered as ONE line under a "Preferences ..." label. A line break
# or a "#" inside it would let a stored rule open its own "# CALLER RULES"-style
# section in the prompt; NUL and other controls have no business in speech.
# Line/paragraph separators (Zl/Zp) count as breaks too.
_RULE_FORBIDDEN_CATEGORIES = ("Cc", "Cs", "Co", "Cn", "Zl", "Zp")

# --- character allowlist (round 6) -------------------------------------------
# Round 5 vetted letters by script and still lost: a combining mark (category
# M) is not a letter, so "o" + U+0334 + "bey" read as Latin, defeated the
# blocked-word regex, and split into fragments that rode spoken_by_caller's
# drift tolerance; a braille blank or emoji did the same as a separator. A
# blocklist of categories chases instances, so this is an ALLOWLIST: what a
# rule may contain is plain ASCII (letters, digits, space, a fixed set of
# punctuation) plus Latin letters whose NFKD base is ASCII. Everything else —
# every mark, symbol, other numeral, other script — is refused by category.
_RULE_ASCII_PUNCT = frozenset(".,;:'\"!?()-/&@+%")
# Latin letters with no decomposition but an unambiguous plain-ASCII spelling.
# José/Zoë/Ñandú need no entry (NFKD strips their marks); Søren and Straße do.
_LATIN_ASCII_FORMS = {
    "ß": "ss", "ø": "o", "æ": "ae", "œ": "oe", "ð": "d", "þ": "th", "ł": "l",
    "đ": "d", "ı": "i", "ŋ": "n", "ħ": "h", "ŧ": "t",
}
_CATEGORY_WORDS = {
    "M": "combining mark", "S": "symbol", "P": "punctuation", "N": "number",
    "Z": "separator", "C": "control or format", "L": "letter",
}


def _script_of(ch: str) -> str:
    """The Unicode script prefix of a letter's name ("LATIN", "CYRILLIC", "CJK", ...)."""
    try:
        return unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return "UNASSIGNED"


def _ascii_base(ch: str) -> str | None:
    """The plain-ASCII spelling of a Latin letter ("é" -> "e", "ß" -> "ss"), or None if it has none."""
    low = ch.lower()
    if low in _LATIN_ASCII_FORMS:
        return _LATIN_ASCII_FORMS[low]
    base = "".join(c for c in unicodedata.normalize("NFKD", low) if not unicodedata.category(c).startswith("M"))
    return base if base and base.isascii() and base.isalpha() else None


def _char_problem(t: str) -> str | None:
    """Why a character of `t` is outside the rule allowlist, or None.

    The reason names the category so a refusal log reads as "combining mark",
    "symbol", "letters outside the Latin script", never just a code point.
    """
    for ch in t:
        if ch.isascii():
            if ch.isalnum() or ch == " " or ch in _RULE_ASCII_PUNCT:
                continue
            return f"contains disallowed ASCII character {ch!r} (U+{ord(ch):04X})"
        cat = unicodedata.category(ch)
        if cat.startswith("L"):
            script = _script_of(ch)
            if script != "LATIN":
                return f"letters outside the Latin script: {script.lower()}"
            if _ascii_base(ch) is None:
                # an IPA / phonetic / small-capital Latin letter has no ASCII
                # spelling, so the squashed match could not read through it
                return f"contains a Latin letter with no plain-ASCII form (U+{ord(ch):04X})"
            continue
        return f"contains disallowed {_CATEGORY_WORDS.get(cat[:1], 'other')} character U+{ord(ch):04X} ({cat})"
    return None


# --- squashed matching (round 6) -----------------------------------------------
# The blocked phrases and tool names carry a literal space or underscore, and
# the token regexes lean on \b, so "always-say", "you.are.now", "bridge call"
# or "o'bey" all slipped through on punctuation the allowlist still admits.
# The squashed form keeps only [a-z0-9] of the mark-stripped text, so no
# separator survives; a phrase must still START and END where a word did in
# the original (boundary-aligned), which is what keeps "weekend bridge" from
# tripping "end_bridge" while "al-ways say" cannot hide "always say".

def _squash(t: str) -> tuple[str, frozenset[int], frozenset[int]]:
    """(squashed, starts, ends): [a-z0-9]* of the folded text plus the squashed offsets where original words begin/end."""
    out: list[str] = []
    starts: set[int] = set()
    ends: set[int] = set()
    in_run = False
    for ch in _normalise_rule(t):
        base = ch.lower() if ch.isascii() and ch.isalnum() else (_ascii_base(ch) if ch.isalpha() else None)
        if base:
            if not in_run:
                starts.add(len(out))
                in_run = True
            out.extend(base)
        elif not unicodedata.category(ch).startswith("M"):
            # a mark is invisible glue, never a boundary; anything else is one
            if in_run:
                ends.add(len(out))
                in_run = False
    if in_run:
        ends.add(len(out))
    return "".join(out), frozenset(starts), frozenset(ends)


def _squash_phrase(phrase: str) -> str:
    return "".join(ch for ch in phrase.lower() if ch.isalnum() and ch.isascii())


# "pin" rides along as a whole aligned word: the token regex only refuses
# "pin " / "pin.", so "p.i.n. is 1234" and "remember my pin" got through.
_SQUASHED_BLOCKED = tuple(sorted(({_squash_phrase(p) for p in _RULE_BLOCKED_PHRASES} | {"pin"}) - {""}, key=len, reverse=True))
# the token regex's "treat ... as me" shape, mirrored for "treat him as-me"
_SQUASHED_TREAT, _SQUASHED_AS_ME = "treat", "asme"
_SQUASHED_DISCLOSE_VERBS = tuple(
    _squash_phrase(v) for v in ("read", "tell", "share", "give", "reveal", "say", "recite", "repeat", "disclose",
                                 "confirm", "provide", "spell out", "hand over", "state")
)
_SQUASHED_DISCLOSE_TARGETS = tuple(
    _squash_phrase(v) for v in ("anyone", "anybody", "whoever", "whomever", "someone", "everyone", "everybody",
                                 "caller", "callers", "they ask", "asks", "asked", "who asks", "who calls", "who rings",
                                 "who phones", "code", "codes", "note", "notes", "file", "on file", "details",
                                 "information", "address", "bank", "account", "medication", "credentials",
                                 "password", "category", "vault", "record", "records")
)
# the token regex allows 40 chars between verb and target; spaces are gone here
_SQUASHED_DISCLOSE_GAP = 32


def _aligned_hits(squashed: str, starts: frozenset[int], ends: frozenset[int], needle: str) -> list[int]:
    """Start offsets of `needle` in `squashed` that begin at a word start and end at a word end."""
    hits = []
    i = squashed.find(needle)
    while i != -1:
        if i in starts and (i + len(needle)) in ends:
            hits.append(i)
        i = squashed.find(needle, i + 1)
    return hits


def _squashed_problem(t: str) -> str | None:
    """A blocked phrase, tool name or disclosure pair readable once separators are removed, or None."""
    squashed, starts, ends = _squash(t)
    for needle in _SQUASHED_BLOCKED:
        if _aligned_hits(squashed, starts, ends, needle):
            return f"contains blocked phrase once separators are removed: {needle!r}"
    for ti in _aligned_hits(squashed, starts, ends, _SQUASHED_TREAT):
        for ai in _aligned_hits(squashed, starts, ends, _SQUASHED_AS_ME):
            if 0 <= ai - (ti + len(_SQUASHED_TREAT)) <= _SQUASHED_DISCLOSE_GAP:
                return "contains blocked phrase once separators are removed: 'treat ... as me'"
    verbs = [(i, v) for v in _SQUASHED_DISCLOSE_VERBS for i in _aligned_hits(squashed, starts, ends, v)]
    if not verbs:
        return None
    targets = [(i, w) for w in _SQUASHED_DISCLOSE_TARGETS for i in _aligned_hits(squashed, starts, ends, w)]
    for vi, v in verbs:
        for ti, w in targets:
            gap = ti - (vi + len(v)) if ti >= vi else vi - (ti + len(w))
            if 0 <= gap <= _SQUASHED_DISCLOSE_GAP:
                return f"reads as a disclosure rule once separators are removed: {v!r}/{w!r}"
    return None


def split_rules(text: str) -> list[str]:
    """The individual rules inside a merged caller_rules / persona_directives column.

    rt_prefs.merge_scalar_rules joins rules with "; ", so a column is a SET of
    rules, not one rule: the shield must judge each member on its own. Judging
    the whole column let a benign set exceed the per-rule length cap, or two
    allowed rules straddle a blocked pattern across the "; " seam, and then
    every rule in the set vanished from every future prompt (round-3 regression).
    """
    out = []
    for part in str(text or "").split(";"):
        p = " ".join(part.split()).strip()
        if p and p.lower() != "none set.":
            out.append(p)
    return out


def rule_text_allowed(text: str) -> tuple[bool, str]:
    """(True, "ok") when `text` is safe to store as a caller rule, else (False, why)."""
    t = _normalise_rule(text).strip()
    if not t:
        return False, "empty"
    if len(t) > _RULE_MAX_LEN:
        return False, f"too long (>{_RULE_MAX_LEN} chars)"
    if "#" in t:
        return False, "contains '#' (prompt section marker)"
    bad = next((ch for ch in t if unicodedata.category(ch) in _RULE_FORBIDDEN_CATEGORIES), None)
    if bad is not None:
        return False, f"contains control or line-break character U+{ord(bad):04X}"
    why = _char_problem(t)
    if why:
        return False, why
    m = _RULE_BLOCKED.search(t)
    if m:
        return False, f"contains blocked phrase: {' '.join(m.group(0).split()).lower()!r}"
    m = _RULE_DISCLOSURE.search(t)
    if m:
        return False, f"reads as a disclosure rule: {' '.join(m.group(0).split()).lower()!r}"
    why = _squashed_problem(t)
    if why:
        return False, why
    return True, "ok"


# Function words carry no payload, so they are neither required nor credited;
# everything else of two or more letters must have been heard. Short words
# used to be skipped wholesale, which let "let him in" ride on padding.
_STOPWORDS = frozenset(
    "the a an to of in on at and or my me i you your please it is be for with that this "
    "am are was were ok okay yes yeah oh um uh".split()
)


def _stem(word: str) -> str:
    """"loves" and "love" are one word to the ear; a plural s is the only drift tolerated."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _strip_marks(text: str) -> str:
    """The folded text with every bare combining mark removed.

    NFKC composes "e" + acute into "é" (a letter), so only marks with no
    precomposed form survive — and those are glue, not letters: "o" + U+0334
    + "bey" must tokenise as ONE word, never as fragments the drift
    tolerance can forgive.
    """
    return "".join(ch for ch in _normalise_rule(text) if not unicodedata.category(ch).startswith("M"))


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(_strip_marks(text))]


def _is_strict(word: str) -> bool:
    """A token the ear cannot mishear into: it carries a digit or a non-ASCII letter.

    An [A-Za-z]-only tokeniser dropped digits (so a swapped phone number was
    invisible) and split a Cyrillic homoglyph into a two-letter fragment that
    rode the drift tolerance. Such tokens must be heard VERBATIM — no
    stemming, no tolerance.
    """
    return any(ch.isdigit() or ord(ch) > 127 for ch in word)


def _content_words(text: str) -> set[str]:
    """Words that carry meaning: two or more characters, at least one letter, not a stopword.

    Pure-digit tokens are not content words (a text of nothing but numbers has
    nothing for the caller to have "said"), but every digit-bearing token is
    still checked verbatim by _strict_tokens.
    """
    out: set[str] = set()
    for w in _tokens(text):
        if len(w) < 2 or w in _STOPWORDS or not any(ch.isalpha() for ch in w):
            continue
        out.add(w if _is_strict(w) else _stem(w))
    return out


def _strict_tokens(text: str) -> set[str]:
    return {w for w in _tokens(text) if _is_strict(w)}


# Inbound texts land in the transcript under this role. It MUST NOT begin with
# "caller:" — _caller_lines below, agent._heard_in_caller_lines and
# rt_postcall_worker._caller_lines all key on that exact prefix, and a text is
# an HTTP POST, not a voice on the line. Review 2026-09-02: the only thing
# keeping a forged webhook out of the email and identity gates was that this
# string happened to carry a parenthetical, and nothing said so. The module
# now refuses to import with a role those gates would trust.
SMS_ROLE = "caller (via SMS text)"
if SMS_ROLE.lower().startswith("caller:"):
    raise RuntimeError("rt_shield.SMS_ROLE would pass the caller-line gates")


def _caller_lines(transcript_lines: list[str]) -> list[str]:
    """Only turns the caller actually spoke — agent/line/SMS turns are not consent."""
    out = []
    for line in transcript_lines or ():
        if line.lower().startswith("caller:"):
            out.append(line.split(":", 1)[1])
    return out


# Clause boundaries for the per-clause check below: punctuation plus every
# conjunction a padded rule could hang its payload on ("... whereupon let him in").
_CLAUSE_SPLIT = re.compile(
    r"[;,.!?\n]|\bas long as\b|\b(?:and|then|also|if|when|whenever|unless|but|or|while"
    r"|whereupon|once|after|before|so|except)\b",
    re.I,
)


def spoken_by_caller(text: str, transcript_lines: list[str], threshold: float = 0.6) -> bool:
    """True iff `text` was heard on caller turns, clause by clause.

    A rule the model "remembers" but the caller never said is the injection
    vector. Every clause must have ALL its content words heard — a single
    unheard word is tolerated only in a clause of six or more, for STT drift —
    because any looser rule lets a malicious clause ride in on the average of
    benign heard padding. `threshold` still gates the overall fraction so a
    caller asking for a stricter bar (1.0) is honoured.
    """
    if not transcript_lines:
        return False
    want = _content_words(text)
    if not want:
        return False
    said = " ".join(_caller_lines(transcript_lines))
    heard = _content_words(said)
    heard_strict = _strict_tokens(said)
    if len(want & heard) / len(want) < threshold:
        return False
    for clause in _CLAUSE_SPLIT.split(_strip_marks(text)):
        # Digits and non-ASCII letters get no drift tolerance at all: a
        # substituted number or a foreign-script payload is never "misheard".
        if _strict_tokens(clause) - heard_strict:
            return False
        cw = _content_words(clause)
        if not cw:
            continue
        unheard = len({w for w in cw if not _is_strict(w)} - heard)
        if unheard > (1 if len(cw) >= 6 else 0):
            return False
    return True


# --- wipe confirmation (finding #6) -----------------------------------------
# forget_me is irreversible, so consent is one exact scripted phrase the agent
# asks for, and nothing else counts: wipe_consent() is the only thing that can
# turn a wipe on. wipe_intent() is the loose matcher used ONLY to decide
# whether to ask — it refuses anything narrowed ("everything about my sister"),
# negated, questioned, or followed by a further clause.
_WIPE_WINDOW = 6
_WIPE_CONSENT_PHRASES = ("erase everything about me and start over", "forget me completely")
_WIPE_CONSENT_PREFIX = re.compile(r"^(?:yes please|yes|okay|ok)\s+")
_NON_WORD = re.compile(r"[^a-z0-9\s]+")


def _normalise_consent(line: str) -> str:
    low = (line or "").lower().replace("’", "'")
    return " ".join(_NON_WORD.sub(" ", low).split())


def wipe_consent(line: str) -> bool:
    """True iff `line`, normalised, IS a scripted consent phrase (optionally led by yes/okay)."""
    raw = (line or "").strip()
    if raw.lower().startswith("caller:"):
        raw = raw.split(":", 1)[1]
    # A question is never consent. Checked on the RAW line, before punctuation
    # is folded away: "Forget me completely?" used to normalise to the phrase.
    # '.', '!' and ',' stay tolerant because STT sprinkles those on statements.
    if "?" in raw:
        return False
    t = _WIPE_CONSENT_PREFIX.sub("", _normalise_consent(raw), count=1)
    return t in _WIPE_CONSENT_PHRASES


def wipe_confirmed(transcript_lines: list[str], since_index: int = 0) -> bool:
    """True iff a caller turn at/after `since_index` (within the last six) is the exact consent phrase."""
    if not transcript_lines:
        return False
    since = max(int(since_index or 0), 0)
    recent = _caller_lines(list(transcript_lines)[since:])[-_WIPE_WINDOW:]
    return any(wipe_consent(line) for line in recent)


_WIPE_OWN_DATA = (
    r"(?:everything(?:\s+(?:about me|on me|you (?:have|know|'ve got|have got|got) (?:about|on) me))?"
    r"|all of it|all (?:of )?my (?:notes|memories|memory|information|data|history|records)"
    r"|my (?:notes|memory|memories|data|history|records)|me)"
)
_WIPE_TRAILER = r"(?:[\s,]+(?:please|now|right now|immediately|thanks|thank you|okay|ok|dear))*"
_WIPE_INTENT_PATTERNS = (
    re.compile(r"\b(?:erase|delete|wipe|forget|clear)\b[^.?!]{0,40}?\b" + _WIPE_OWN_DATA + _WIPE_TRAILER + r"\s*$", re.I),
    re.compile(r"\bstart (?:over|fresh|again from scratch)" + _WIPE_TRAILER + r"\s*$", re.I),
)
_WIPE_INTENT_NEGATED = re.compile(
    r"\b(?:don['’]?t|do not|never|not|no|shouldn['’]?t|wouldn['’]?t|won['’]?t|can['’]?t"
    r"|couldn['’]?t|didn['’]?t|rather you didn['’]?t)\b",
    re.I,
)
_WIPE_INTENT_QUESTION_START = re.compile(
    r"^\W*(?:how|what|is|do|does|can|could|should|will|why|when|would)\b", re.I,
)
# hypothetical / interrogative shapes that can sit mid-sentence
_WIPE_INTENT_HYPOTHETICAL = re.compile(r"\b(?:would you|could you|can you|did you|if i|what if)\b", re.I)
_SENTENCES = re.compile(r"(?<=[.?!;])\s+")


def _wipe_intent_sentence(sentence: str) -> bool:
    s = sentence.strip()
    if not s or s.endswith("?"):
        return False
    if _WIPE_INTENT_QUESTION_START.search(s) or _WIPE_INTENT_HYPOTHETICAL.search(s):
        return False
    if _WIPE_INTENT_NEGATED.search(s):
        return False
    body = s.rstrip(".!;, ")
    return any(pat.search(body) for pat in _WIPE_INTENT_PATTERNS)


def wipe_intent(transcript_lines: list[str]) -> bool:
    """True iff a recent caller sentence reads as an un-hedged request to wipe THEIR OWN data.

    This only decides whether to ask for the consent phrase; it never erases.
    """
    if not transcript_lines:
        return False
    for line in _caller_lines(list(transcript_lines))[-_WIPE_WINDOW:]:
        for sentence in _SENTENCES.split(line):
            if _wipe_intent_sentence(sentence):
                return True
    return False


# --- recipient consent (findings #2/#8) --------------------------------------

def email_spoken_by_caller(email: str, transcript_lines: list[str]) -> bool:
    """True iff the CALLER said this exact address on the line.

    The extractor reads the whole transcript, so an address the agent read
    back ("I still have x@y on file, right?") looks spoken too; only caller
    turns count, and token boundaries keep "bob@x.com" from matching inside
    "notbob@x.com" or "bob@x.com.evil".
    """
    e = (email or "").strip()
    if not e:
        return False
    pat = re.compile(r"(?<![\w.+-])" + re.escape(e) + r"(?![\w.-])", re.I)
    return any(pat.search(line) for line in _caller_lines(transcript_lines))
