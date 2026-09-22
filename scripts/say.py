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

Windows only, or WSL with powershell.exe on the path; elsewhere it prints
the message and exits 0 so nothing upstream breaks.
"""

import argparse
import os
import shutil
import subprocess
import sys

# One PowerShell invocation per message. The text and the voice go in as
# environment variables, never pasted into the script: PowerShell ends a
# single-quoted string at a curly quote as well as a straight one, so
# "Don’t push" with only the straight quotes escaped closed the string
# early and ran the rest of the sentence as code.
_SPEAK = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "if ($env:SAY_VOICE) {{"
    " $v = $s.GetInstalledVoices() | Where-Object {{"
    " $_.VoiceInfo.Name.IndexOf($env:SAY_VOICE,"
    " [StringComparison]::OrdinalIgnoreCase) -ge 0 }} | Select-Object -First 1;"
    " if ($v) {{ $s.SelectVoice($v.VoiceInfo.Name) }} }};"
    "$s.Rate = {rate};"
    "$s.Speak($env:SAY_TEXT);"
    "$s.Dispose()"
)
_LIST = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name };"
    "$s.Dispose()"
)


def _powershell() -> str | None:
    """The PowerShell to speak through: Windows' own, or Windows' from WSL."""
    if os.name == "nt":
        return "powershell"
    return shutil.which("powershell.exe")


def _ps(exe: str, script: str, wait: bool, env: dict | None = None) -> int:
    cmd = [exe, "-NoProfile", "-NonInteractive", "-Command", script]
    if env:
        extra = env
        env = dict(os.environ, **extra)
        # WSL hands a Windows process only the variables WSLENV names.
        if os.name != "nt":
            env["WSLENV"] = ":".join(
                [v for v in [os.environ.get("WSLENV")] if v] + list(extra))
    if wait:
        return subprocess.run(cmd, check=False, env=env).returncode
    # Detached, so the caller returns while the message plays. Output is
    # discarded: there is nothing useful in it, and a pipe nobody reads
    # would stall the synthesizer.
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env=env,
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

    exe = _powershell()
    if exe is None:
        if args.list:
            print("no Windows speech voices on this platform")
        else:
            print(" ".join(args.text) or sys.stdin.read().strip())
        return 0

    if args.list:
        return _ps(exe, _LIST, wait=True)

    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        p.error("nothing to say")
    if not -10 <= args.rate <= 10:
        p.error("rate is -10 to 10")

    return _ps(exe, _SPEAK.format(rate=args.rate), wait=args.wait,
               env={"SAY_TEXT": text, "SAY_VOICE": args.voice or ""})


if __name__ == "__main__":
    sys.exit(main())
