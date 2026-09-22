"""Read a short message aloud to the driver, through the Windows voices.

A driver cannot read a chat window at 260 km/h. This says the message
instead, over whatever the game's audio is playing, using the speech
synthesizer Windows ships with -- no package to install, no account, no
network. The voices are the ones under Settings > Time & Language > Speech;
"Microsoft David Desktop" and "Microsoft Zira Desktop" are on every
install, and any others added there are listed by --list.

    python scripts/say.py "Box this lap. Rear pressures to nineteen."
    python scripts/say.py --voice Zira --rate 1 "Bias fifty-eight is on."
    echo "Lap one forty-four eight. Keep going." | python scripts/say.py
    python scripts/say.py --list

Keep it to a sentence or two: a message that takes longer to say than a
straight does is one the driver will stop listening to. Numbers are read
as digits, so write them the way you would say them on the radio --
"one forty-four eight", "fifty-eight", "nineteen psi" -- rather than as
1:44.8.

Returns as soon as the speech has been handed off, so a session can keep
working while it plays; --wait blocks until it has finished, for when two
messages must not overlap.

Windows only; elsewhere it prints the message and exits 0 so nothing
upstream breaks.
"""

import argparse
import os
import subprocess
import sys

# One PowerShell invocation per message, holding the whole script in the
# -Command argument. The text goes in as a single-quoted PowerShell literal
# with the quotes doubled, which is the only escaping that string needs.
_SPEAK = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "{select}"
    "$s.Rate = {rate};"
    "$s.Speak('{text}');"
    "$s.Dispose()"
)
_LIST = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name };"
    "$s.Dispose()"
)


def _ps(script: str, wait: bool) -> int:
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    if wait:
        return subprocess.run(cmd, check=False).returncode
    # Detached, so the caller returns while the message plays. Output is
    # discarded: there is nothing useful in it, and a pipe nobody reads
    # would stall the synthesizer.
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text", nargs="*", help="what to say (or pipe it on stdin)")
    p.add_argument("--voice", help="a substring of the voice name, e.g. Zira")
    p.add_argument("--rate", type=int, default=0,
                   help="speaking rate, -10 (slow) to 10 (fast); default 0")
    p.add_argument("--wait", action="store_true",
                   help="return only when the message has finished playing")
    p.add_argument("--list", action="store_true", help="list installed voices")
    args = p.parse_args(argv)

    if os.name != "nt":
        if args.list:
            print("no Windows speech voices on this platform")
        else:
            print(" ".join(args.text) or sys.stdin.read().strip())
        return 0

    if args.list:
        return _ps(_LIST, wait=True)

    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        p.error("nothing to say")
    if not -10 <= args.rate <= 10:
        p.error("rate is -10 to 10")

    select = ""
    if args.voice:
        name = args.voice.replace("'", "''")
        select = ("$v = $s.GetInstalledVoices() | Where-Object { "
                  f"$_.VoiceInfo.Name -like '*{name}*' }} | Select-Object -First 1;"
                  "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };")
    script = _SPEAK.format(select=select, rate=args.rate,
                           text=text.replace("'", "''"))
    return _ps(script, wait=args.wait)


if __name__ == "__main__":
    sys.exit(main())
