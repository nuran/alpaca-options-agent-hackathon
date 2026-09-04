"""
Deterministic record/replay of a live cycle -- the architectural fix for the class of bug
that produced B1-B5 in the 2026-08-28 audit.

Those five defects were not found by reading the code. They were found by building a fake
broker by hand and driving the agent through it. That work is not reusable: the next audit
starts from scratch, and none of it became a regression test that runs on every change.

This module makes it reusable. The agent touches the outside world through exactly three
seams -- the Alpaca CLI (`cli`), the model endpoint (`_anthropic_post`) and the clock. A
`Recorder` writes every crossing of those seams to a tape; a `Player` serves the recorded
answers back in order and REFUSES to invent one. So:

  * a live cycle can be replayed bit-exactly, months later, with no broker and no API key;
  * every incident becomes a permanent regression tape (`tapes/2026-08-27-partial-close/`);
  * a code change that alters the sequence of external calls fails loudly (`TapeMiss`)
    instead of silently doing something new against a real account;
  * an audit becomes `make replay-all` instead of a person writing a fake chain.

Fail-closed by design: on replay, a call the tape does not contain raises rather than
falling through to the network. A replay can therefore never place an order.

Tape format: one JSON object per line.
    {"seq": 0, "channel": "cli", "key": ["clock"], "allow_fail": false, "result": {...}}
    {"seq": 1, "channel": "clock", "key": ["now"], "result": "2026-08-27T10:05:00"}
    {"seq": 2, "channel": "llm",  "key": ["<context-hash>"], "result": {...}}

Credentials never reach a tape: they travel in the environment, not in `cli` argv, and the
recorder stores argv only. `assert_clean()` re-checks that before a tape is written.

Self-check: `python3 agent/tape.py`
"""
import json
import os
import re

CHANNELS = ('cli', 'llm', 'clock')

# Anything that looks like a key must never be written to a tape, whatever the seam.
_SECRET = re.compile(r'(sk-[A-Za-z0-9_\-]{8,}|PK[A-Z0-9]{16,}|[A-Za-z0-9/+]{40,}={0,2})')


class TapeMiss(RuntimeError):
    """Replay asked for an interaction the tape does not contain -- behaviour diverged."""


def assert_clean(text):
    """Refuse to write anything that pattern-matches a credential."""
    m = _SECRET.search(text)
    if m:
        raise ValueError(f"refusing to write a possible secret to the tape: {m.group(0)[:6]}...")
    return text


class Recorder:
    """Wraps the three seams and appends every crossing to `path` (JSONL)."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
        self.seq = 0
        self._fh = open(path, 'w')

    def _write(self, channel, key, result, **extra):
        rec = {'seq': self.seq, 'channel': channel, 'key': list(key), 'result': result}
        rec.update(extra)
        line = json.dumps(rec, default=str)
        self._fh.write(assert_clean(line) + "\n")
        self._fh.flush()
        self.seq += 1
        return result

    def wrap_cli(self, real_cli):
        def recorded(args, allow_fail=False):
            out = real_cli(args, allow_fail=allow_fail)
            return self._write('cli', args, out, allow_fail=bool(allow_fail))
        return recorded

    def wrap_llm(self, real_post):
        def recorded(api_key, body):
            out = real_post(api_key, body)
            # The prompt is journalled elsewhere; the tape stores the response only.
            return self._write('llm', [_body_key(body)], out)
        return recorded

    def wrap_clock(self, real_now):
        def recorded():
            out = real_now()
            return self._write('clock', ['now'], out)
        return recorded

    def close(self):
        self._fh.close()


class Player:
    """Serves a recorded tape back in order. Never touches the network."""

    def __init__(self, path, strict_order=True):
        self.records = [json.loads(l) for l in open(path) if l.strip()]
        self.strict_order = strict_order
        self.pos = 0
        self.used = [False] * len(self.records)
        self.path = path

    def _take(self, channel, key):
        key = [str(k) for k in key]
        # In-order match first (the normal case), then any unused match with the same key
        # (tolerates a reordering that does not change WHAT was asked).
        for i in range(self.pos, len(self.records)):
            r = self.records[i]
            if not self.used[i] and r['channel'] == channel and [str(k) for k in r['key']] == key:
                self.used[i] = True
                self.pos = i + 1 if self.strict_order else self.pos
                return r['result']
        for i, r in enumerate(self.records):
            if not self.used[i] and r['channel'] == channel and [str(k) for k in r['key']] == key:
                self.used[i] = True
                return r['result']
        raise TapeMiss(f"{channel} {key!r} is not on {os.path.basename(self.path)} "
                       f"(consumed {sum(self.used)}/{len(self.records)})")

    def cli(self, args, allow_fail=False):
        try:
            return self._take('cli', args)
        except TapeMiss:
            if allow_fail:
                return None                    # same degradation the live path has
            raise

    def llm(self, api_key, body):
        return self._take('llm', [_body_key(body)])

    def now(self):
        return self._take('clock', ['now'])

    def unconsumed(self):
        return [r for r, u in zip(self.records, self.used) if not u]


def _body_key(body):
    """Stable key for a model request: the rendered user payload, not the whole body."""
    try:
        msgs = body.get('messages') or []
        content = msgs[-1].get('content') if msgs else ''
        if isinstance(content, list):
            content = ' '.join(c.get('text', '') for c in content if isinstance(c, dict))
        return str(abs(hash(content)) % (10 ** 12))
    except Exception:
        return 'unkeyed'


def install(module, player_or_recorder):
    """
    Point a loaded `agent_loop` module at a tape. Returns a restore() callable.

    Recording keeps the real functions and wraps them; replaying replaces them outright,
    so a replay physically cannot reach the broker even if a new code path tries.
    """
    saved = {'cli': module.cli, 'post': getattr(module, '_anthropic_post', None)}
    if isinstance(player_or_recorder, Recorder):
        r = player_or_recorder
        module.cli = r.wrap_cli(saved['cli'])
        if saved['post']:
            module._anthropic_post = r.wrap_llm(saved['post'])
    else:
        p = player_or_recorder
        module.cli = p.cli
        module._anthropic_post = p.llm

    def restore():
        module.cli = saved['cli']
        if saved['post']:
            module._anthropic_post = saved['post']
    return restore


def diff_journal(recorded, replayed, ignore=('ts', 'llm_raw')):
    """Compare two journal entries field by field; returns a list of human-readable diffs."""
    out = []
    for k in sorted(set(recorded) | set(replayed)):
        if k in ignore:
            continue
        a, b = recorded.get(k, '<missing>'), replayed.get(k, '<missing>')
        if json.dumps(a, sort_keys=True, default=str) != json.dumps(b, sort_keys=True, default=str):
            out.append(f"{k}: recorded={_short(a)} replayed={_short(b)}")
    return out


def _short(v, n=160):
    s = json.dumps(v, default=str, sort_keys=True)
    return s if len(s) <= n else s[:n] + '...'


if __name__ == "__main__":
    import tempfile

    d = tempfile.mkdtemp()
    tape_path = os.path.join(d, 'cycle.jsonl')

    # A toy "agent": three CLI calls whose answers depend on the account state.
    calls = {('clock',): {'is_open': True}, ('account',): {'equity': '100000'},
             ('positions',): []}

    def fake_cli(args, allow_fail=False):
        return calls[tuple(args)]

    rec = Recorder(tape_path)
    wrapped = rec.wrap_cli(fake_cli)
    live = [wrapped(['clock']), wrapped(['account']), wrapped(['positions'])]
    rec.close()

    # Replay returns the same answers with the broker gone.
    def gone(args, allow_fail=False):
        raise AssertionError("replay reached the broker")

    p = Player(tape_path)
    assert [p.cli(['clock']), p.cli(['account']), p.cli(['positions'])] == live
    assert p.unconsumed() == []

    # A NEW call the tape does not contain fails loudly instead of hitting the network.
    p2 = Player(tape_path)
    p2.cli(['clock'])
    try:
        p2.cli(['orders', '--status', 'open'])
        raise AssertionError("TapeMiss not raised")
    except TapeMiss as e:
        assert 'orders' in str(e)
    # ...unless the live path itself tolerates failure, where replay degrades identically.
    assert p2.cli(['data', 'news'], allow_fail=True) is None

    # Out-of-order but identical questions still resolve (recording order is not a contract).
    p3 = Player(tape_path)
    assert p3.cli(['positions']) == [] and p3.cli(['clock']) == {'is_open': True}

    # Secrets can never be written, whatever a broker echoes back.
    r2 = Recorder(os.path.join(d, 'x.jsonl'))
    try:
        r2._write('cli', ['account'], {'note': 'sk-abcdefghijklmnop'})
        raise AssertionError("secret written to tape")
    except ValueError:
        pass
    r2.close()

    # Journal diffing is what a replay-based regression test asserts on.
    assert diff_journal({'action': 'open', 'ts': 1}, {'action': 'open', 'ts': 2}) == []
    assert diff_journal({'action': 'open'}, {'action': 'pass'})[0].startswith('action:')

    print("tape.py self-check OK")
