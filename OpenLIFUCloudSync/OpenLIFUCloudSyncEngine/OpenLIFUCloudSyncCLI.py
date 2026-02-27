#!/usr/bin/env python
import sys
import time
import argparse
import logging
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='[ENGINE] %(asctime)s - %(levelname)s - %(message)s',
        stream=sys.stdout
    )

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

            print(f"NEW_ID_TOKEN:{current_id_token}")
            print(f"NEW_EXPIRY:{token_expiry}")
            return True
        except Exception as e:
            print(f"TOKEN_REFRESH_FAILED:{e}")
            return False

    if not refresh_session():
        sys.exit(1)

    if not Cloud:
        logging.error(
            "The 'openlifu' library was not found in the PYTHONPATH.")
        sys.exit(1)

    cloud = None
    try:
        logging.info(f"Initializing Sync Engine for: {args.db_path}")
        cloud = Cloud()

        def on_cloud_status(status_obj):
            """
            Callback from the Cloud class.
            status_obj is an instance of the Status class.
            """
            current_status = status_obj.status

            if current_status == Status.STATUS_IDLE:
                print(f"SYNC_COMPLETED_AT:{time.strftime('%H:%M:%S')}")
            else:
                print(f"CLOUD_STATUS:{current_status}")

            sys.stdout.flush()

        cloud.set_status_callback(on_cloud_status)

        db_path = Path(args.db_path).resolve()

        cloud.set_access_token(current_id_token)
        cloud.start(db_path)

        logging.info("Performing initial synchronization...")
        cloud.sync()
        cloud.start_background_sync()
        logging.info("Entering background monitor mode.")
        while True:
            if time.time() > (token_expiry - 300):
                if refresh_session():
                    cloud.set_access_token(current_id_token)

            time.sleep(1)

    except Exception as e:
        logging.error(f"Fatal Engine Error: {e}")
        sys.exit(1)
    finally:
        if cloud:
            logging.info("Shutting down Cloud connection...")
            cloud.stop()


if __name__ == "__main__":
    main()
