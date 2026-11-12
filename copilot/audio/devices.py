"""Audio device discovery + the Step 1 check CLI.

    python -m copilot.audio.devices            # list devices, then live level meters
    python -m copilot.audio.devices --list     # list only
    python -m copilot.audio.devices --seconds 20

Resolution rules (see CLAUDE.md §5.1):
- ``them`` = system audio loopback.
    macOS   → virtual device "BlackHole 2ch" (system output must be a Multi-Output Device
              that includes BlackHole, otherwise the channel is silent).
    Windows → WASAPI loopback of the default output device (via pyaudiowpatch).
    Linux   → a PulseAudio/PipeWire ".monitor" source if PortAudio exposes one.
- ``me``   = default input device (microphone), or a device chosen by name.

PortAudio (sounddevice) is imported lazily so that the pure-DSP parts of the package
stay importable on machines without it.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Literal

Platform = Literal["darwin", "win32", "linux"]


class AudioDeviceError(RuntimeError):
    """Raised with a human-readable, actionable message."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float

    @property
    def is_input(self) -> bool:
        return self.max_input_channels > 0

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.index}] {self.name} ({self.hostapi}, in={self.max_input_channels}, out={self.max_output_channels}, {int(self.default_samplerate)} Hz)"


@dataclass(frozen=True)
class SourceSpec:
    """What capture.py needs to open one channel."""

    role: Literal["them", "me"]
    backend: Literal["sounddevice", "wasapi_loopback"]
    device: AudioDevice | None  # None for wasapi_loopback (resolved inside pyaudiowpatch)
    note: str = ""


MAC_BLACKHOLE_HELP = """\
BlackHole 2ch не найден среди аудиоустройств.

Чтобы копилот слышал системный звук на macOS:
  1. brew install --cask blackhole-2ch      (спросит пароль администратора)
  2. Audio MIDI Setup → «+» (внизу слева) → Create Multi-Output Device
  3. Отметь свои наушники/колонки И «BlackHole 2ch» (у BlackHole включи Drift Correction)
  4. Правый клик по Multi-Output Device → Use This Device For Sound Output
Затем перезапусти проверку: python -m copilot.audio.devices
"""

WIN_LOOPBACK_HELP = """\
Для захвата системного звука на Windows нужен пакет pyaudiowpatch:
  uv add pyaudiowpatch     (или: uv sync --extra win)
"""

LINUX_LOOPBACK_HELP = """\
На Linux системный звук берётся с PulseAudio/PipeWire monitor-источника.
Укажи его явно в config.yaml → audio.them_device (имя содержит ".monitor"),
или запусти с переменной окружения PULSE_SOURCE=<sink>.monitor и them_device: "pulse".
"""


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #
def _sd():
    try:
        import sounddevice as sd  # noqa: WPS433 (lazy import by design)
    except OSError as exc:  # PortAudio shared library missing
        raise AudioDeviceError(
            "PortAudio не найден. macOS: `brew install portaudio` (обычно не нужно — "
            "wheel sounddevice уже содержит библиотеку); Linux: `apt install libportaudio2`.\n"
            f"Исходная ошибка: {exc}"
        ) from exc
    return sd


def list_devices() -> list[AudioDevice]:
    sd = _sd()
    hostapis = sd.query_hostapis()
    out: list[AudioDevice] = []
    for idx, dev in enumerate(sd.query_devices()):
        out.append(
            AudioDevice(
                index=idx,
                name=str(dev["name"]),
                hostapi=str(hostapis[dev["hostapi"]]["name"]),
                max_input_channels=int(dev["max_input_channels"]),
                max_output_channels=int(dev["max_output_channels"]),
                default_samplerate=float(dev["default_samplerate"]),
            )
        )
    return out


def default_input_index() -> int | None:
    sd = _sd()
    idx = sd.default.device[0]
    return None if idx is None or idx < 0 else int(idx)


def default_output_device() -> AudioDevice | None:
    sd = _sd()
    idx = sd.default.device[1]
    if idx is None or idx < 0:
        return None
    devs = list_devices()
    return devs[int(idx)] if 0 <= int(idx) < len(devs) else None


# --------------------------------------------------------------------------- #
# Pure selection logic (unit-tested with fake device lists)
# --------------------------------------------------------------------------- #
def pick_by_name(devices: list[AudioDevice], hint: str, *, need_input: bool = True) -> AudioDevice | None:
    """Case-insensitive match: exact name first, then substring. Prefers input-capable devices."""
    hint_l = hint.strip().lower()
    if not hint_l:
        return None
    pool = [d for d in devices if (d.is_input if need_input else True)]
    for d in pool:
        if d.name.lower() == hint_l:
            return d
    for d in pool:
        if hint_l in d.name.lower():
            return d
    return None


def pick_mac_loopback(devices: list[AudioDevice]) -> AudioDevice | None:
    inputs = [d for d in devices if d.is_input]
    for d in inputs:
        if d.name.lower() == "blackhole 2ch":
            return d
    for d in inputs:
        if "blackhole" in d.name.lower():
            return d
    return None


def pick_linux_monitor(devices: list[AudioDevice]) -> AudioDevice | None:
    inputs = [d for d in devices if d.is_input]
    for d in inputs:
        if ".monitor" in d.name.lower() or "monitor of" in d.name.lower():
            return d
    return None


def pick_microphone(devices: list[AudioDevice], default_idx: int | None) -> AudioDevice | None:
    """Default input device unless it is a loopback (BlackHole/monitor) — then first real mic."""

    def is_loopback(d: AudioDevice) -> bool:
        n = d.name.lower()
        return "blackhole" in n or ".monitor" in n or "loopback" in n

    if default_idx is not None and 0 <= default_idx < len(devices):
        d = devices[default_idx]
        if d.is_input and not is_loopback(d):
            return d
    for d in devices:
        if d.is_input and not is_loopback(d):
            return d
    return None


# --------------------------------------------------------------------------- #
# Resolution against the real system
# --------------------------------------------------------------------------- #
def resolve_them(hint: str = "auto", platform: Platform | None = None) -> SourceSpec:
    platform = platform or _platform()
    devices = list_devices()

    if hint and hint.lower() != "auto":
        dev = pick_by_name(devices, hint)
        if dev is None:
            raise AudioDeviceError(
                f"Устройство для канала them с именем «{hint}» не найдено.\n" + _format_list(devices)
            )
        return SourceSpec("them", "sounddevice", dev)

    if platform == "darwin":
        dev = pick_mac_loopback(devices)
        if dev is None:
            raise AudioDeviceError(MAC_BLACKHOLE_HELP + "\n" + _format_list(devices))
        return SourceSpec("them", "sounddevice", dev, note=_mac_output_note())

    if platform == "win32":
        try:
            import pyaudiowpatch  # noqa: F401
        except ImportError as exc:
            raise AudioDeviceError(WIN_LOOPBACK_HELP) from exc
        return SourceSpec("them", "wasapi_loopback", None, note="WASAPI loopback of the default output")

    dev = pick_linux_monitor(devices)
    if dev is None:
        raise AudioDeviceError(LINUX_LOOPBACK_HELP + "\n" + _format_list(devices))
    return SourceSpec("them", "sounddevice", dev)


def resolve_me(hint: str = "auto") -> SourceSpec:
    devices = list_devices()
    if hint and hint.lower() != "auto":
        dev = pick_by_name(devices, hint)
        if dev is None:
            raise AudioDeviceError(
                f"Микрофон с именем «{hint}» не найден.\n" + _format_list(devices)
            )
        return SourceSpec("me", "sounddevice", dev)
    dev = pick_microphone(devices, default_input_index())
    if dev is None:
        raise AudioDeviceError("Не найдено ни одного устройства ввода (микрофона).\n" + _format_list(devices))
    return SourceSpec("me", "sounddevice", dev)


def _mac_output_note() -> str:
    """Warn when the system output does not look like a Multi-Output device (then them=silence)."""
    try:
        out = default_output_device()
    except AudioDeviceError:
        return ""
    if out is None:
        return ""
    n = out.name.lower()
    if "multi-output" in n or "aggregate" in n or "blackhole" in n:
        return f"system output: {out.name}"
    return (
        f"ВНИМАНИЕ: системный вывод сейчас «{out.name}», а не Multi-Output Device — "
        "в BlackHole ничего не попадёт, канал them будет тихим. См. README → macOS."
    )


def _platform() -> Platform:
    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("win"):
        return "win32"
    return "linux"


def _format_list(devices: list[AudioDevice]) -> str:
    lines = ["Доступные устройства:"]
    for d in devices:
        tag = "in " if d.is_input else "out"
        lines.append(f"  {tag} {d}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI: list + live meters
# --------------------------------------------------------------------------- #
def _bar(db: float, width: int = 24) -> str:
    # −60 dBFS … 0 dBFS → 0 … width
    frac = max(0.0, min(1.0, (db + 60.0) / 60.0))
    n = int(round(frac * width))
    return "█" * n + "·" * (width - n)


def main(argv: list[str] | None = None) -> int:
    from copilot.audio.capture import open_sources
    from copilot.audio.dsp import dbfs
    from copilot.config import load_config

    ap = argparse.ArgumentParser(description="List audio devices and show live levels for them/me.")
    ap.add_argument("--list", action="store_true", help="only list devices")
    ap.add_argument("--seconds", type=float, default=0, help="meter duration (0 = until Ctrl+C)")
    args = ap.parse_args(argv)

    try:
        devices = list_devices()
    except AudioDeviceError as exc:
        print(exc, file=sys.stderr)
        return 2

    print(_format_list(devices))
    di = default_input_index()
    do = default_output_device()
    print(f"\nDefault input : {devices[di].name if di is not None else '—'}")
    print(f"Default output: {do.name if do else '—'}")

    if args.list:
        return 0

    cfg = load_config()
    try:
        sources = open_sources(cfg)
    except AudioDeviceError as exc:
        print("\n" + str(exc), file=sys.stderr)
        return 2

    print("\nSources:")
    for role, src in sources.items():
        print(f"  {role:<4} ← {src.describe()}")
        if src.spec.note:
            print(f"         {src.spec.note}")
    print("\nВключи что-нибудь со звуком (YouTube) и скажи пару слов в микрофон. Ctrl+C — стоп.\n")

    for src in sources.values():
        src.start()

    t0 = time.monotonic()
    frames = {role: 0 for role in sources}
    peak = {role: -90.0 for role in sources}
    try:
        while True:
            time.sleep(0.1)
            parts = []
            for role, src in sources.items():
                while True:  # drain the queue so the meter reflects data actually delivered
                    frame = src.get_nowait()
                    if frame is None:
                        break
                    frames[role] += 1
                db = dbfs(src.level)
                peak[role] = max(peak[role], db)
                parts.append(f"{role:<4} {_bar(db)} {db:6.1f} dB  {frames[role]:6d} fr")
            sys.stdout.write("\r" + "   ".join(parts))
            sys.stdout.flush()
            if args.seconds and time.monotonic() - t0 >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        for src in sources.values():
            src.stop()

    print("\n\nИтог:")
    ok = True
    for role, src in sources.items():
        got = frames[role] > 0
        loud = peak[role] > -50.0
        status = "OK" if (got and loud) else ("ТИХО — фреймы идут, но звука нет" if got else "НЕТ ДАННЫХ")
        ok &= got and loud
        print(f"  {role:<4}: {status}  (peak {peak[role]:.1f} dBFS, frames {frames[role]}, errors {src.errors})")
    if not ok:
        print(
            "\nЕсли them тихий на macOS: проверь, что системный вывод = Multi-Output Device с BlackHole."
            "\nЕсли me тихий: System Settings → Privacy & Security → Microphone → разреши терминалу."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
