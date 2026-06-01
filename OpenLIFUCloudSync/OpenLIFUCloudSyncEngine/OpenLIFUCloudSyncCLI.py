#!/usr/bin/env python
import sys
import time
import argparse
import logging
import threading
from pathlib import Path

import requests

from openlifu.cloud.cloud import Cloud
from openlifu.cloud.status import Status


def main():
    parser = argparse.ArgumentParser(
        description="OpenLIFU Background Sync Engine")
    parser.add_argument(
        "--db_path", help="Path to local database", required=True)
    parser.add_argument(
        "--api_key", help="Cloud Access Token", required=True)
    parser.add_argument("--refresh_token", required=True)
    parser.add_argument("--env", default="prod", help="Sync environment")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='[ENGINE] %(asctime)s - %(levelname)s - %(message)s',
        stream=sys.stdout
    )

    # The openlifu library logs every REST call at INFO level via the
    # "Cloud" logger (CLOUD_LOG: ...). That floods the terminal during
    # background sync, so suppress everything below WARNING here. Real
    # problems (e.g. WS_FATAL_ERROR, request errors) still come through.
    logging.getLogger("Cloud").setLevel(logging.WARNING)

    logging.info(f"Starting OpenLIFU Cloud Sync Engine in '{args.env}' environment")

    # Force unbuffered output so parent process sees logs immediately
    sys.stdout.reconfigure(line_buffering=True)

    current_id_token = None
    token_expiry = 0

    def refresh_session():
        nonlocal current_id_token, token_expiry
        url = f"https://securetoken.googleapis.com/v1/token?key={args.api_key}"
        try:
            r = requests.post(url, data={
                "grant_type": "refresh_token",
                "refresh_token": args.refresh_token
            }, timeout=10)
            r.raise_for_status()
            data = r.json()
            current_id_token = data['id_token']
            token_expiry = time.time() + int(data['expires_in'])

            # NOTE: these prints are a control-protocol with the parent
            # process (see onProcessOutput in OpenLIFUCloudSync.py). They
            # MUST be raw stdout writes (not logging calls) so the lines
            # start with the literal sentinel and have no prefix.
            print(f"NEW_ID_TOKEN:{current_id_token}")
            print(f"NEW_EXPIRY:{token_expiry}")
            return True
        except Exception as e:
            logging.error(f"TOKEN_REFRESH_FAILED:{e}")
            return False

    if not refresh_session():
        sys.exit(1)

    if not Cloud:
        logging.error(
            "The 'openlifu' library was not found in the PYTHONPATH.")
        sys.exit(1)

    # Graceful-shutdown protocol: the parent process closes our stdin
    # (or writes the line "STOP") to ask us to wind down. A daemon
    # thread watches stdin and sets this Event; the main loop checks it
    # and falls through to the ``finally`` clause so cloud.stop() runs.
    stop_event = threading.Event()

    def _watch_stdin():
        try:
            for line in sys.stdin:
                if line.strip().upper() == "STOP":
                    break
        except Exception:
            pass
        stop_event.set()

    threading.Thread(target=_watch_stdin, daemon=True).start()

    cloud = None
    try:
        logging.info(f"Initializing Sync Engine for: {args.db_path}")
        cloud = Cloud(args.env)

        # Dedupe so we only emit a status line on actual transitions --
        # otherwise the cloud library's per-poll callbacks produce a
        # flood of identical CLOUD_STATUS:synchronizing lines.
        last_status = None

        def on_cloud_status(status_obj):
            """
            Callback from the Cloud class.
            status_obj is an instance of the Status class.
            """
            nonlocal last_status
            current_status = status_obj.status
            if current_status == last_status:
                return
            last_status = current_status

            # SYNC_COMPLETED_AT and CLOUD_STATUS are control-protocol
            # messages parsed by the parent process; keep them as plain
            # prints (no logging prefix).
            if current_status == Status.STATUS_IDLE:
                print(f"SYNC_COMPLETED_AT:{time.strftime('%H:%M:%S')}")
                logging.info("Sync complete.")
            else:
                print(f"CLOUD_STATUS:{current_status}")
                logging.debug(f"Cloud status: {current_status}")

            sys.stdout.flush()

        cloud.set_status_callback(on_cloud_status)

        db_path = Path(args.db_path).resolve()

        cloud.set_access_token(current_id_token)
        cloud.start(db_path)

        logging.info("Sync started.")
        # start_background_sync() performs an initial sync itself, so
        # don't double-sync here -- a separate cloud.sync() call would
        # produce a redundant "Sync complete." before the background
        # loop kicks off.
        cloud.start_background_sync()
        logging.debug("Entering background monitor mode.")
        while not stop_event.is_set():
            if time.time() > (token_expiry - 300):
                if refresh_session():
                    cloud.set_access_token(current_id_token)

            # Use Event.wait so we react to a stop request immediately
            # rather than after up to a full second of sleep.
            stop_event.wait(timeout=1)

        logging.info("Stop requested; shutting down sync...")

    except KeyboardInterrupt:
        logging.info("Interrupted; shutting down sync...")
    except Exception as e:
        logging.error(f"Fatal Engine Error: {e}")
        sys.exit(1)
    finally:
        if cloud:
            try:
                cloud.stop()
                logging.info("Sync stopped cleanly.")
            except Exception as e:
                logging.warning(f"Error during cloud shutdown: {e}")


if __name__ == "__main__":
    main()
