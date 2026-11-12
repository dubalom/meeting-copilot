"""Entry point: ``python -m copilot <command>`` (or ``uv run copilot <command>``).

    check                       readiness report: config, keys, context/, audio devices
    devices [--list]            audio devices + live level meters (step 1 check)
    serve                       run the server (HTTP + WebSocket) for browser / CLI clients
    cli [--url WS] [--no-me]    live transcript in the terminal (local capture → server)
    replay FILE.wav [--speed]   feed a WAV file as the `them` channel → transcript
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # backwards compatibility with step-1 flags
    if argv and argv[0] in ("--check", "--cli"):
        argv[0] = argv[0][2:]

    ap = argparse.ArgumentParser(prog="copilot", description="Meeting Copilot")
    ap.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("check", help="readiness report")
    p_dev = sub.add_parser("devices", help="audio devices + level meters")
    p_dev.add_argument("--list", action="store_true")
    p_dev.add_argument("--seconds", type=float, default=0)

    p_srv = sub.add_parser("serve", help="run the server")
    p_srv.add_argument("--host")
    p_srv.add_argument("--port", type=int)
    p_srv.add_argument("--reload", action="store_true")
    p_srv.add_argument("--open", action="store_true", help="open the web UI in the browser")

    p_cli = sub.add_parser("cli", help="live transcript in the terminal")
    p_cli.add_argument("--url", help="ws://host:port of a running server (default: embedded)")
    p_cli.add_argument("--no-me", action="store_true", help="don't capture/transcribe the microphone")
    p_cli.add_argument("--session", help="session id to join/resume")
    p_cli.add_argument("--mic-only", action="store_true", help="only the microphone (no system audio / BlackHole)")

    p_imp = sub.add_parser("import-chats", help="import chat exports (claude.ai / ChatGPT / Telegram / .md) into your private store")
    p_imp.add_argument("paths", nargs="+", type=Path, help="export .zip, folder, conversations.json, result.json, .md ...")
    p_imp.add_argument("--user", default="owner", help="whose private store (default: owner = you)")
    p_srch = sub.add_parser("search-chats", help="test search over your private chats")
    p_srch.add_argument("query")
    p_srch.add_argument("--user", default="owner")
    p_srch.add_argument("-k", type=int, default=5)

    p_rep = sub.add_parser("replay", help="WAV file → transcript")
    p_rep.add_argument("wav", type=Path)
    p_rep.add_argument("--url")
    p_rep.add_argument("--speed", type=float, default=1.0, help="playback speed (1 = real time)")
    p_rep.add_argument("--channel", choices=["them", "me"], default="them")
    p_rep.add_argument("--ask", action="store_true", help="after the file ends, ask for an answer to the last question")
    p_rep.add_argument("--summary", action="store_true", help="after the file ends, request meeting notes")

    args = ap.parse_args(argv)

    from copilot.app import setup_logging
    from copilot.config import load_config

    setup_logging(args.verbose)
    cfg = load_config(args.config)

    if args.cmd in (None, "check"):
        from copilot.app import App

        ok, report = App(cfg).readiness_report()
        print(report)
        if args.cmd is None:
            print("\nКоманды: check | devices | serve | cli | replay FILE.wav   (python -m copilot -h)")
        return 0 if ok else 1

    if args.cmd == "devices":
        from copilot.audio.devices import main as devices_main

        return devices_main((["--list"] if args.list else []) + ["--seconds", str(args.seconds)])

    if args.cmd == "import-chats":
        from copilot.knowledge import KnowledgeStore, import_path

        store = KnowledgeStore(cfg.knowledge.dir, args.user)
        docs = []
        for p in args.paths:
            if not p.expanduser().exists():
                print(f"Нет такого пути: {p}", file=sys.stderr)
                return 2
            found = import_path(p)
            print(f"{p}: найдено {len(found)} документов")
            docs.extend(found)
        if not docs:
            print("Ничего не импортировано: не узнал формат. Поддерживаются выгрузки claude.ai, ChatGPT, Telegram (JSON), .md/.txt.")
            return 1
        added, total = store.add(docs)
        st = store.stats()
        print(f"Готово: +{added}, всего {total} документов ({st['chunks']} фрагментов) · {st['by_source']}")
        print(f"Хранится локально: {store.path}  (доступно только пользователю «{args.user}»)")
        return 0

    if args.cmd == "search-chats":
        from copilot.knowledge import KnowledgeStore

        store = KnowledgeStore(cfg.knowledge.dir, args.user)
        if not store.path.exists():
            print("Личная база пуста: сначала python -m copilot import-chats <выгрузка>")
            return 1
        for h in store.search(args.query, k=args.k):
            c = h.chunk
            print(f"[{h.score:5.1f}] {c.title} ({c.date}) · {c.source}\n        {c.text[:220].replace(chr(10), ' ')}…")
        return 0

    if args.cmd == "serve":
        from copilot.server.api import run_server

        if args.host:
            cfg.server.host = args.host
        if args.port:
            cfg.server.port = args.port
        missing = cfg.secrets.missing_for(stt_provider=cfg.stt.provider, brain_provider="agent_sdk")
        if missing:
            print(f"Нет {', '.join(missing)} в .env — STT не заработает.", file=sys.stderr)
        run_server(cfg, reload=args.reload, open_browser=args.open)
        return 0

    if args.cmd in ("cli", "replay"):
        from copilot.cli.terminal import live_pump, run_client, wav_pump

        if not cfg.secrets.deepgram_api_key and not getattr(args, "url", None):
            print("Нет DEEPGRAM_API_KEY в .env", file=sys.stderr)
            return 2
        try:
            if args.cmd == "cli":
                transcribe_me = args.mic_only or (cfg.audio.transcribe_me and not args.no_me)
                cfg.audio.transcribe_me = transcribe_me
                pump = live_pump(cfg, mic_only=args.mic_only)
                channels = ["me"] if args.mic_only else (["them", "me"] if transcribe_me else ["them"])
                coro = run_client(cfg, pump, url=args.url, transcribe_me=transcribe_me, channels=channels,
                                  session_id=args.session)
            else:
                if not args.wav.exists():
                    print(f"Файл не найден: {args.wav}", file=sys.stderr)
                    return 2
                pump = wav_pump(args.wav, channel=args.channel, speed=args.speed)
                coro = run_client(cfg, pump, url=args.url, transcribe_me=args.channel == "me",
                                  channels=[args.channel], linger_s=2.0,
                                  ask_after=args.ask, summary_after=args.summary)
            return asyncio.run(coro)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # AudioDeviceError etc. — readable message, no traceback
            if args.verbose:
                raise
            print(f"\n{exc}", file=sys.stderr)
            return 2

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
