"""
Replay a recorded cycle and assert the agent still decides the same thing.

    python3 agent/replay.py tapes/2026-08-27_partial-close      # one tape
    python3 agent/replay.py --all                               # every tape (make replay)
    python3 agent/replay.py <dir> --update                      # re-bless expected.json

A tape bundle is a directory:

    tapes/<name>/
        tape.jsonl          every crossing of the broker / model / clock seams
        rules.json          the rule set in force for that cycle
        state_before.json   the structure registry as it was at cycle start
        expected.json       the journal entry the cycle produced  (the assertion)
        README.md           what this tape captures and why it is kept

Why this exists: the five defects in the 2026-08-28 audit were found by driving the agent
through a hand-built fake broker, and that work evaporated when the audit ended. A tape turns
one such investigation into a permanent test. `make replay` runs them all in about a second,
with no account, no API key and no network -- a replay literally cannot place an order,
because the broker seam is replaced by the tape rather than wrapped.

What a failure means:
  * a journal diff  -> the agent would now decide differently on a situation we have seen;
  * a TapeMiss      -> the agent now ASKS something new (a call the tape does not contain),
                       which is a behavioural change even if the decision is unchanged.
Neither is automatically a bug. Both must be looked at, and `--update` records the new
answer deliberately.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tape as TAPE

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
TAPES_DIR = os.path.join(REPO_ROOT, 'tapes')


def run_tape(bundle, update=False):
    """Replay one bundle. Returns (ok, diffs, entry)."""
    import agent_loop as A

    with open(os.path.join(bundle, 'rules.json')) as f:
        rules = json.load(f)
    state_before = {}
    sb = os.path.join(bundle, 'state_before.json')
    if os.path.exists(sb):
        with open(sb) as f:
            state_before = json.load(f)

    work = tempfile.mkdtemp(prefix='replay-')
    saved_dir, saved_bin = A.JOURNAL_DIR, A.ALPACA_BIN
    A.JOURNAL_DIR = work
    A.ALPACA_BIN = '/nonexistent-broker'          # belt and braces: no CLI can resolve
    with open(os.path.join(work, 'state.json'), 'w') as f:
        json.dump(state_before, f)

    meta_path = os.path.join(bundle, 'meta.json')
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    shadow = bool(meta.get('shadow', True))       # a replay never trades; shadow is the default

    player = TAPE.Player(os.path.join(bundle, 'tape.jsonl'))
    restore = TAPE.install(A, player)
    try:
        A.cycle(rules, shadow, False)
    finally:
        restore()
        A.JOURNAL_DIR, A.ALPACA_BIN = saved_dir, saved_bin

    entries = []
    for name in sorted(os.listdir(work)):
        if name.endswith('.jsonl'):
            with open(os.path.join(work, name)) as f:
                entries += [json.loads(l) for l in f if l.strip()]
    shutil.rmtree(work, ignore_errors=True)
    entry = entries[-1] if entries else {}

    exp_path = os.path.join(bundle, 'expected.json')
    if update or not os.path.exists(exp_path):
        with open(exp_path, 'w') as f:
            json.dump(entry, f, indent=2, default=str, sort_keys=True)
        return True, [], entry
    with open(exp_path) as f:
        expected = json.load(f)
    diffs = TAPE.diff_journal(expected, entry)
    left = player.unconsumed()
    if left:
        diffs.append(f"{len(left)} recorded interaction(s) were never requested: "
                     f"{[r['key'] for r in left][:4]}")
    return not diffs, diffs, entry


def main(argv):
    update = '--update' in argv
    targets = [a for a in argv if not a.startswith('--')]
    if '--all' in argv or not targets:
        if not os.path.isdir(TAPES_DIR):
            print(f"no tapes yet: record one with `alpaca-agent --shadow --record "
                  f"tapes/<name>/tape.jsonl`")
            return 0
        targets = [os.path.join(TAPES_DIR, n) for n in sorted(os.listdir(TAPES_DIR))
                   if os.path.isdir(os.path.join(TAPES_DIR, n))]
    if not targets:
        print("no tape bundles found")
        return 0

    failed = 0
    for b in targets:
        name = os.path.basename(b.rstrip('/'))
        try:
            ok, diffs, _ = run_tape(b, update)
        except TAPE.TapeMiss as e:
            ok, diffs = False, [f"TapeMiss: {e}"]
        except Exception as e:                                  # noqa: BLE001 -- report, don't crash the suite
            ok, diffs = False, [f"{type(e).__name__}: {e}"]
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        for d in diffs:
            print(f"      {d}")
        failed += 0 if ok else 1
    print(f"\n{len(targets) - failed}/{len(targets)} tapes reproduced")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
